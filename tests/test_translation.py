from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.ai import translation


def test_contains_chinese():
    assert translation.contains_chinese("日志分析")
    assert not translation.contains_chinese("log analysis")


@pytest.mark.asyncio
async def test_translate_query_uses_openai_compatible_response():
    translation._cache.clear()
    settings = MagicMock()
    settings.model.base_url = "http://model"
    settings.model.api_key = "token"
    settings.model.name = "translator"
    settings.model.timeout = 5
    completion = AsyncMock(return_value="log analysis")
    with (
        patch.object(translation, "get_settings", return_value=settings),
        patch.object(translation, "chat_completion", completion),
    ):
        assert await translation.translate_query_to_english(" 日志分析 ") == "log analysis"
        assert await translation.translate_query_to_english("日志分析") == "log analysis"
        assert completion.await_count == 1


@pytest.mark.asyncio
async def test_translate_query_failure_degrades_to_none():
    settings = MagicMock()
    settings.model.base_url = "http://model"
    settings.model.api_key = "token"
    settings.model.timeout = 5
    completion = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    with (
        patch.object(translation, "get_settings", return_value=settings),
        patch.object(translation, "chat_completion", completion),
    ):
        assert await translation.translate_query_to_english("分析日志") is None
        assert completion.await_count == 1
