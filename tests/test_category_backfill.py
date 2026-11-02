import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ai import category_backfill
from src.ai.category_backfill import BackfillStats, LockBusyError


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def row(name: str = "skill-a") -> category_backfill._UnclassifiedRow:
    return category_backfill._UnclassifiedRow(
        id=uuid.uuid4(),
        name=name,
        content="body",
        extra_metadata={"category": "Some Random Category"},
    )


def lock_session_with(locked: bool) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar.return_value = locked
    session.execute.return_value = result
    return session


@pytest.mark.asyncio
async def test_backfill_fills_empty_categories_and_advances_cursor(monkeypatch):
    lock_session = lock_session_with(True)
    monkeypatch.setattr(
        category_backfill,
        "AsyncSessionLocal",
        lambda: AsyncContext(lock_session),
    )
    rows = [row("a"), row("b")]
    fetch = AsyncMock(side_effect=[rows, []])
    monkeypatch.setattr(category_backfill, "_fetch_chunk", fetch)
    classify = AsyncMock(return_value=[(rows[0].id, "Development and Build")])
    monkeypatch.setattr(category_backfill, "_classify_rows", classify)
    write = AsyncMock(return_value=1)
    monkeypatch.setattr(category_backfill, "_write_categories", write)

    # chunk_size=2 与批行数相同 → 整批处理完继续拉下一批，可观察游标推进
    stats = await category_backfill.backfill_missing_categories(chunk_size=2)

    assert stats == BackfillStats(total=2, filled=1, unclassified=0, failed=0)
    # 游标推进到本批最后一行 id
    assert fetch.await_args_list[1].args[0] == rows[-1].id
    write.assert_awaited_once()
    # 锁 + 解锁各一次
    assert lock_session.execute.await_count == 2


@pytest.mark.asyncio
async def test_backfill_raises_lock_busy_and_does_not_unlock(monkeypatch):
    lock_session = lock_session_with(False)
    monkeypatch.setattr(
        category_backfill,
        "AsyncSessionLocal",
        lambda: AsyncContext(lock_session),
    )

    with pytest.raises(LockBusyError):
        await category_backfill.backfill_missing_categories()

    # 只有 try_lock 一次，未持有锁就不解锁
    assert lock_session.execute.await_count == 1


@pytest.mark.asyncio
async def test_backfill_skips_when_classifier_unavailable(monkeypatch):
    lock_session = lock_session_with(True)
    monkeypatch.setattr(
        category_backfill,
        "AsyncSessionLocal",
        lambda: AsyncContext(lock_session),
    )
    monkeypatch.setattr(category_backfill, "_build_classifier", lambda: None)

    stats = await category_backfill.backfill_missing_categories()

    assert stats.skipped == "category classifier unavailable"
    assert stats.total == 0


@pytest.mark.asyncio
async def test_classify_rows_counts_failures_and_unclassified():
    rows = [row("a"), row("b"), row("c")]

    def classify(**kwargs):
        if kwargs["skill_file"].parent.name == "a":
            raise RuntimeError("llm down")
        if kwargs["skill_file"].parent.name == "b":
            return None
        return "Development and Build"

    classifier = MagicMock()
    classifier.classify = classify

    stats = BackfillStats()
    pairs = await category_backfill._classify_rows(rows, classifier, stats)

    assert pairs == [(rows[2].id, "Development and Build")]
    assert stats.failed == 1
    assert stats.unclassified == 1


@pytest.mark.asyncio
async def test_write_categories_updates_only_still_empty_rows(monkeypatch):
    """条件更新（AND category IS NULL OR ''）：已分类行由 DB 拒绝写入。"""
    session = AsyncMock()
    first = MagicMock()
    first.rowcount = 1
    second = MagicMock()
    second.rowcount = 0  # 行在写入前已被其他任务填上，条件更新跳过
    commit = AsyncMock()
    session.execute = AsyncMock(side_effect=[first, second])
    session.commit = commit
    monkeypatch.setattr(
        category_backfill,
        "AsyncSessionLocal",
        lambda: AsyncContext(session),
    )

    pairs = [(uuid.uuid4(), "Development and Build"), (uuid.uuid4(), "others")]
    updated = await category_backfill._write_categories(pairs)

    assert updated == 1
    commit.assert_awaited_once()
    for call in session.execute.await_args_list:
        sql = call.args[0].text if hasattr(call.args[0], "text") else str(call.args[0])
        assert "category IS NULL" in sql
