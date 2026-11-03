"""Shared logger for the SkillSpector LLM review."""

from __future__ import annotations

import logging
from typing import Any


class _AuditReviewLogAdapter(logging.LoggerAdapter):
    """给本模块所有日志消息统一加 "AuditLLMReview: " 前缀。"""

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        return f"AuditLLMReview: {msg}", kwargs


logger: logging.LoggerAdapter[None] = _AuditReviewLogAdapter(
    logging.getLogger("src.jobs.audit_llm_review"), {}
)
