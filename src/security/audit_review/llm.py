"""OpenAI-compatible chat completion client used by the review job."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from src.ai.llm import chat_completion
from src.core.config import AuditLLMReviewConfig
from src.security.audit_review.constants import (
    _RETRYABLE_STATUS,
    _TRANSIENT_ATTEMPTS,
)
from src.security.audit_review.log import get_logger
from src.security.audit_review.metrics import current_audit
from src.security.audit_review.prompt import (
    _EMPTY_VERDICT_REMINDER,
    SYSTEM_PROMPT,
    LLMReviewError,
    _extract_json_object,
    _normalise_verdicts,
)
from src.security.audit_review.queue import _RateLimiter

logger = get_logger("llm")


async def call_llm_json(
    client: httpx.AsyncClient,
    limiter: _RateLimiter,
    prompt: str,
    expected: int,
    post: Callable[..., Awaitable[str]],
    system_prompt: str = SYSTEM_PROMPT,
) -> list[dict[str, Any]]:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]
    content = await post(client, limiter, messages)
    verdicts = _normalise_verdicts(_extract_json_object(content))
    if verdicts:
        return verdicts

    # 合法但空的判定不能当成“全部误报”，追加纠正提示重试一次
    logger.debug("empty verdicts for expected=%d; retrying with reminder", expected)
    retry_messages = [
        messages[0],
        {"role": "user", "content": prompt + _EMPTY_VERDICT_REMINDER},
    ]
    content = await post(client, limiter, retry_messages)
    verdicts = _normalise_verdicts(_extract_json_object(content))
    if not verdicts and expected > 0:
        raise LLMReviewError("model returned no verdicts after reminder")
    return verdicts


async def post_chat(
    client: httpx.AsyncClient,
    limiter: _RateLimiter,
    *,
    config: AuditLLMReviewConfig,
    api_key: str,
    messages: list[dict[str, str]],
) -> str:
    """带限流与退避重试的 JSON 模式调用：HTTP 细节复用 ``src.ai.llm``。

    复核特有的只有限流预留、可重试状态码与退避策略；请求体、鉴权头、
    响应解析都走共享封装，避免两套实现漂移。

    日志只记录批大小/耗时/重试/错误类型，不打印 prompt 与响应原文，避免把
    skill 源码与安全报告写入日志造成磁盘与泄密风险。
    """
    estimated_tokens = (
        sum(len(message.get("content") or "") for message in messages) // 4
        + config.max_output_tokens
    )
    record = current_audit.get()
    if record is not None:
        record.estimated_tokens += estimated_tokens

    last_error = ""
    for attempt in range(_TRANSIENT_ATTEMPTS):
        await limiter.acquire(estimated_tokens)
        call_started = time.monotonic()
        try:
            return await chat_completion(
                messages,
                model=config.model,
                base_url=config.base_url,
                api_key=api_key,
                client=client,
                max_tokens=config.max_output_tokens,
                timeout=config.timeout,
                response_format={"type": "json_object"},
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in _RETRYABLE_STATUS:
                logger.warning(
                    "LLM request failed: HTTP %d (retryable=false, tokens~%d)",
                    status,
                    estimated_tokens,
                )
                raise LLMReviewError(
                    f"HTTP {status}: {exc.response.text[:300]}"
                ) from exc
            last_error = f"HTTP {status}: {exc.response.text[:200]}"
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        except ValueError as exc:
            # 响应不是合法 JSON：与网络错误一样按瞬时失败重试
            last_error = f"invalid JSON response: {exc}"
        finally:
            if record is not None:
                elapsed = time.monotonic() - call_started
                record.llm_calls += 1
                record.llm_seconds += elapsed
                if elapsed >= config.slow_llm_seconds:
                    record.slow_llm_calls += 1
                    logger.warning(
                        "slow LLM call: %.2fs (threshold=%.2fs, tokens~%d)",
                        elapsed,
                        config.slow_llm_seconds,
                        estimated_tokens,
                    )
        if attempt + 1 < _TRANSIENT_ATTEMPTS:
            backoff = 3.0 * (attempt + 1)
            if record is not None:
                record.llm_retries += 1
            logger.warning(
                "LLM retry %d/%d after %s; backoff=%.0fs",
                attempt + 1,
                _TRANSIENT_ATTEMPTS - 1,
                last_error[:160],
                backoff,
            )
            await asyncio.sleep(backoff)
    logger.warning(
        "LLM request failed after %d attempts: %s",
        _TRANSIENT_ATTEMPTS,
        last_error[:200],
    )
    raise LLMReviewError(f"LLM request failed after retries: {last_error}")
