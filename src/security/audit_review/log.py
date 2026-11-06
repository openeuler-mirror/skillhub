"""Shared logger for the SkillSpector LLM review.

所有复核相关日志统一使用 ``[AuditLLMReview]`` 前缀，并自动带上当前轮次的
``run id``（通过 ContextVar 注入），便于按轮次 grep 与关联调度器/审计/LLM
调用各层日志。子模块通过 :func:`get_logger` 获取各自 logger，保留 ``%(name)s``
的模块来源信息，同时共享同一套前缀逻辑。
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

PREFIX = "[AuditLLMReview]"

# 当前复核轮次标识；每轮 run_once 开始时设置，轮次结束后重置。
RUN_ID: ContextVar[str] = ContextVar("audit_review_run_id", default="")


class _AuditReviewLogAdapter(logging.LoggerAdapter):
    """给日志消息统一加 ``[AuditLLMReview]`` 前缀与当前 run id。"""

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        run_id = RUN_ID.get()
        prefix = f"{PREFIX} run={run_id}" if run_id else PREFIX
        return f"{prefix} {msg}", kwargs


def get_logger(name: str = "scheduler") -> logging.LoggerAdapter[None]:
    """按子模块名返回带统一前缀的 logger。

    底层 logger 名为 ``src.jobs.audit_llm_review.<name>``，父级与调度器一致，
    既能在 api.log 的 ``%(name)s`` 中区分来源，又共享同一套日志级别配置。
    """
    return _AuditReviewLogAdapter(
        logging.getLogger(f"src.jobs.audit_llm_review.{name}"), {}
    )


def set_run_id(run_id: str) -> Any:
    """设置当前上下文的轮次标识，返回可用于恢复的 token。"""
    return RUN_ID.set(run_id)


def reset_run_id(token: Any) -> None:
    """恢复上一轮次标识（与 :func:`set_run_id` 的 token 配对使用）。"""
    RUN_ID.reset(token)


logger = get_logger("scheduler")
