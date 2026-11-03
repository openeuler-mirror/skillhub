"""LLM 客户端响应解析与重试测试。"""

import pytest

from src.security.audit_review.constants import DEFAULT_VERDICT_CONFIDENCE
from src.security.audit_review.llm import call_llm_json
from src.security.audit_review.prompt import SYSTEM_PROMPT
from src.security.audit_review.queue import _RateLimiter


class TestCallLLM:
    @pytest.mark.asyncio
    async def test_call_llm_uses_system_prompt(self):
        captured: dict[str, str] = {}

        async def fake_post(client, limiter, messages):
            captured["system"] = messages[0]["content"]
            return '{"findings":[{"index":1,"is_vulnerability":true,"reason":"x"}]}'

        verdicts = await call_llm_json(
            None,
            _RateLimiter(rpm=0, tpm=0),
            "prompt",
            1,
            post=fake_post,
        )
        assert captured["system"] == SYSTEM_PROMPT
        assert verdicts[0]["is_vulnerability"] is True
        assert verdicts[0]["confidence"] == DEFAULT_VERDICT_CONFIDENCE
