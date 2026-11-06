"""OpenAI 兼容 LLM 统一调用封装（skill 分类 / 搜索翻译 / 安全审计复核共用）。

收敛此前散落在 ``src/ai/category_classifier.py`` 与
``src/ai/translation.py`` 的重复实现：请求体构造、``enable_thinking`` 开关、
鉴权、响应解析、耗时日志各写一遍，改一处漏一处（翻译就曾漏掉关闭思考，
被 Qwen3 的长思维链拖慢到 30-45s）。

调用方自行决定异常处理：
- 分类：让它抛出，交给外层重试/降级
- 翻译：捕获后记日志，跳过翻译腿
- 复核：捕获后按状态码/网络错误退避重试
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from src.core.config import get_settings

_logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 128
DEFAULT_TEMPERATURE = 0.0

_async_client: httpx.AsyncClient | None = None


def _chat_url(base_url: str | None = None) -> str:
    base = base_url or get_settings().model.base_url
    return f"{base.rstrip('/')}/chat/completions"


def _auth_headers(api_key: str | None = None) -> dict[str, str]:
    key = api_key or get_settings().model.api_key
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def build_chat_payload(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    enable_thinking: bool = False,
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 /chat/completions 请求体。

    ``enable_thinking=False`` 默认关闭思考：Qwen3/推理系列默认先输出长思维链，
    分类、翻译这类短任务用不上还拖慢调用。硅基流动仅识别**顶层**
    ``enable_thinking``（``chat_template_kwargs`` 写法无效）；不识别该参数的
    模型会静默忽略，无需区分 provider。

    ``response_format`` 用于需要结构化输出的调用方（如安全审计复核的
    ``json_object`` 模式）；不传时不写入请求体，保持旧行为。
    """
    payload: dict[str, Any] = {
        "model": model or get_settings().model.name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "enable_thinking": enable_thinking,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    return payload


def extract_chat_content(data: dict[str, Any]) -> str:
    """从 OpenAI 兼容响应中取出 assistant 文本（去掉思维链等额外字段）。

    兼容 ``content`` 为字符串或分段列表（部分 provider 会把多段文本放进
    list）两种形态；结构缺失时返回空串，由调用方决定是否当作失败处理。
    """
    choices = data.get("choices") if isinstance(data, dict) else None
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    return str(content or "").strip()


def chat_completion_sync(
    messages: list[dict[str, str]],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: float | None = None,
    enable_thinking: bool = False,
) -> str:
    """同步调用（供 skillcrawler 的同步扫描线程使用），异常原样抛出。"""
    settings = get_settings()
    payload = build_chat_payload(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        enable_thinking=enable_thinking,
    )
    started_at = time.perf_counter()
    status: int | str = "request_error"
    try:
        with httpx.Client(timeout=timeout or settings.model.timeout) as client:
            response = client.post(
                _chat_url(), headers=_auth_headers(), json=payload,
            )
            status = response.status_code
            response.raise_for_status()
            data = response.json()
    finally:
        _logger.debug(
            "LLM chat: model=%s status=%s elapsed=%.3fs",
            payload.get("model"),
            status,
            time.perf_counter() - started_at,
        )
    return extract_chat_content(data)


async def chat_completion(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: float | None = None,
    enable_thinking: bool = False,
    response_format: dict[str, Any] | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """异步调用，异常原样抛出。

    默认使用 ``settings.model`` 与进程级 client；需要独立 provider 或连接池的
    调用方（如安全审计复核）可显式传 ``model`` / ``base_url`` / ``api_key`` /
    ``client``，无需另写一套 HTTP 请求与响应解析。
    """
    settings = get_settings()
    payload = build_chat_payload(
        messages,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        enable_thinking=enable_thinking,
        response_format=response_format,
    )
    started_at = time.perf_counter()
    status: int | str = "request_error"
    try:
        response = await (client or _get_async_client()).post(
            _chat_url(base_url),
            headers=_auth_headers(api_key),
            json=payload,
            timeout=timeout or settings.model.timeout,
        )
        status = response.status_code
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            _logger.debug(
                "LLM usage: prompt=%s completion=%s",
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
            )
    finally:
        _logger.debug(
            "LLM chat: model=%s status=%s elapsed=%.3fs",
            payload.get("model"),
            status,
            time.perf_counter() - started_at,
        )
    return extract_chat_content(data)


def _get_async_client() -> httpx.AsyncClient:
    global _async_client
    if _async_client is None:
        _async_client = httpx.AsyncClient(timeout=30.0)
    return _async_client


async def close_llm_service() -> None:
    """关闭进程级 LLM HTTP 客户端。"""
    global _async_client
    if _async_client is not None:
        await _async_client.aclose()
        _async_client = None
