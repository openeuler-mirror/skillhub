"""OpenAI-compatible chat completion client used by the review job."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from src.core.config import AuditLLMReviewConfig
from src.security.audit_review.constants import (
    _RETRYABLE_STATUS,
    _TRANSIENT_ATTEMPTS,
)
from src.security.audit_review.log import logger
from src.security.audit_review.prompt import (
    _EMPTY_VERDICT_REMINDER,
    SYSTEM_PROMPT,
    LLMReviewError,
    _extract_json_object,
    _normalise_verdicts,
)
from src.security.audit_review.queue import _RateLimiter


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
    url = f"{config.base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": config.max_output_tokens,
        # Qwen3.x 关闭思考模式（不支持的模型会忽略该参数）
        "enable_thinking": False,
        "response_format": {"type": "json_object"},
    }
    estimated_tokens = (
        sum(len(message.get("content") or "") for message in messages) // 4
        + config.max_output_tokens
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error = ""
    for attempt in range(_TRANSIENT_ATTEMPTS):
        await limiter.acquire(estimated_tokens)
        try:
            response = await client.post(url, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code in _RETRYABLE_STATUS:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            elif response.status_code >= 400:
                raise LLMReviewError(f"HTTP {response.status_code}: {response.text[:300]}")
            else:
                try:
                    data = response.json()
                except ValueError as exc:
                    last_error = f"invalid JSON response: {exc}"
                else:
                    usage = data.get("usage") if isinstance(data, dict) else None
                    if isinstance(usage, dict):
                        logger.debug(
                            "LLM usage: prompt=%s completion=%s",
                            usage.get("prompt_tokens"),
                            usage.get("completion_tokens"),
                        )
                    return extract_message_content(data)
        if attempt + 1 < _TRANSIENT_ATTEMPTS:
            await asyncio.sleep(3.0 * (attempt + 1))
    raise LLMReviewError(f"LLM request failed after retries: {last_error}")


def extract_message_content(data: Any) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMReviewError("unexpected chat completion payload") from exc
    if isinstance(content, list):
        content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return str(content or "").strip()
