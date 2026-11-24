"""worker 下游任务启动时序的回归测试。

覆盖：
1. discover 首轮完成前，LLM 审核不启动；
2. 首轮完成后启动 LLM 审核，并返回其调度器引用；
3. LLM 审核启动失败被隔离，worker 仍能继续运行；
4. LLM 审核关闭时，不等待 discover 首轮，worker 可正常继续启动。

license 定时任务已移除，本文件不再覆盖 license 调度器。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import src.jobs.worker as worker


def _settings(
    *,
    discover_enabled: bool = True,
    audit_enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        security=SimpleNamespace(enable_audit=False),
        discover_scheduler=SimpleNamespace(enabled=discover_enabled),
        audit_llm_review=SimpleNamespace(enabled=audit_enabled),
    )


async def test_audit_starts_after_discover_first_round():
    """首轮完成前不启动 audit；完成后启动并返回调度器引用。"""
    discover = SimpleNamespace(first_round_done=asyncio.Event())
    discover_started = asyncio.Event()
    events: list[object] = []

    async def start_discover():
        events.append("discover_start")
        discover_started.set()
        return discover

    async def start_audit():
        events.append(("audit", discover.first_round_done.is_set()))
        return "audit-scheduler"

    with (
        patch.object(worker, "settings", _settings()),
        patch.object(worker, "run_migration", AsyncMock()),
        patch.object(worker, "start_discover_scheduler", start_discover),
        patch.object(worker, "start_audit_llm_review_scheduler", start_audit),
    ):
        task = asyncio.create_task(worker._run_background_tasks())
        await asyncio.wait_for(discover_started.wait(), timeout=1)

        # discover 首轮尚未结束：audit 不能启动
        assert events == ["discover_start"]

        discover.first_round_done.set()
        result = await asyncio.wait_for(task, timeout=2)

    assert ("audit", True) in events
    assert result == (None, discover, "audit-scheduler")


async def test_audit_start_failure_is_isolated():
    """audit 启动异常被隔离，worker 仍返回已启动的调度器引用。"""
    discover = SimpleNamespace(first_round_done=asyncio.Event())

    async def start_discover():
        # 模拟首轮已完成，直接进入下游启动
        discover.first_round_done.set()
        return discover

    async def start_audit():
        raise RuntimeError("audit start boom")

    with (
        patch.object(worker, "settings", _settings()),
        patch.object(worker, "run_migration", AsyncMock()),
        patch.object(worker, "start_discover_scheduler", start_discover),
        patch.object(worker, "start_audit_llm_review_scheduler", start_audit),
    ):
        result = await asyncio.wait_for(worker._run_background_tasks(), timeout=2)

    assert result == (None, discover, None)


async def test_audit_disabled_does_not_wait_for_discover():
    """audit 关闭时，不等待 discover 首轮。"""
    discover = SimpleNamespace(first_round_done=asyncio.Event())

    async def start_discover():
        return discover

    with (
        patch.object(
            worker,
            "settings",
            _settings(audit_enabled=False),
        ),
        patch.object(worker, "run_migration", AsyncMock()),
        patch.object(worker, "start_discover_scheduler", start_discover),
    ):
        # 不 set first_round_done，也能在超时内返回
        result = await asyncio.wait_for(worker._run_background_tasks(), timeout=1)

    assert result == (None, discover, None)
