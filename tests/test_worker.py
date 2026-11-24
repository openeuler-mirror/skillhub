"""worker 下游任务启动时序的回归测试。

覆盖：
1. discover 首轮完成前，license / LLM 审核都不启动；
2. 首轮完成后两者并行启动，license 拿到同一个 discover 调度器
   （用于后续跟随每一轮 discover 触发）；
3. license 启动失败不阻断 LLM 审核（gather return_exceptions）；
4. 下游任务全部关闭时，不等待 discover 首轮，worker 可正常继续启动。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import src.jobs.worker as worker


def _settings(
    *,
    discover_enabled: bool = True,
    license_enabled: bool = True,
    audit_enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        security=SimpleNamespace(enable_audit=False),
        discover_scheduler=SimpleNamespace(enabled=discover_enabled),
        license_check=SimpleNamespace(enabled=license_enabled),
        audit_llm_review=SimpleNamespace(enabled=audit_enabled),
    )


async def test_downstream_schedulers_start_after_discover_first_round():
    """首轮完成前不启动下游；完成后并行启动 license 与 audit。"""
    discover = SimpleNamespace(first_round_done=asyncio.Event())
    discover_started = asyncio.Event()
    events: list[object] = []

    async def start_discover():
        events.append("discover_start")
        discover_started.set()
        return discover

    async def start_license(ds):
        events.append(
            ("license", ds is discover, discover.first_round_done.is_set())
        )
        return "license-scheduler"

    async def start_audit():
        events.append(("audit", discover.first_round_done.is_set()))
        return "audit-scheduler"

    with (
        patch.object(worker, "settings", _settings()),
        patch.object(worker, "run_migration", AsyncMock()),
        patch.object(worker, "start_discover_scheduler", start_discover),
        patch.object(worker, "start_license_check_scheduler", start_license),
        patch.object(worker, "start_audit_llm_review_scheduler", start_audit),
    ):
        task = asyncio.create_task(worker._run_background_tasks())
        await asyncio.wait_for(discover_started.wait(), timeout=1)

        # discover 首轮尚未结束：下游任务一个都不能启动
        assert events == ["discover_start"]

        discover.first_round_done.set()
        result = await asyncio.wait_for(task, timeout=2)

    # license 拿到同一个 discover 调度器，且两者都在首轮完成后启动
    assert ("license", True, True) in events
    assert ("audit", True) in events
    assert result == (
        None,
        discover,
        "license-scheduler",
        "audit-scheduler",
    )


async def test_license_start_failure_does_not_block_audit():
    """license 启动异常被隔离，audit 仍能启动。"""
    discover = SimpleNamespace(first_round_done=asyncio.Event())
    audit_started: list[bool] = []

    async def start_discover():
        # 模拟首轮已完成，直接进入下游启动
        discover.first_round_done.set()
        return discover

    async def start_license(ds):
        raise RuntimeError("license start boom")

    async def start_audit():
        audit_started.append(True)
        return "audit-scheduler"

    with (
        patch.object(worker, "settings", _settings()),
        patch.object(worker, "run_migration", AsyncMock()),
        patch.object(worker, "start_discover_scheduler", start_discover),
        patch.object(worker, "start_license_check_scheduler", start_license),
        patch.object(worker, "start_audit_llm_review_scheduler", start_audit),
    ):
        result = await asyncio.wait_for(worker._run_background_tasks(), timeout=2)

    assert audit_started == [True]
    assert result[2] is None
    assert result[3] == "audit-scheduler"


async def test_no_downstream_tasks_does_not_wait_for_discover():
    """license 与 audit 都关闭时，不等待 discover 首轮。"""
    discover = SimpleNamespace(first_round_done=asyncio.Event())

    async def start_discover():
        return discover

    with (
        patch.object(
            worker,
            "settings",
            _settings(license_enabled=False, audit_enabled=False),
        ),
        patch.object(worker, "run_migration", AsyncMock()),
        patch.object(worker, "start_discover_scheduler", start_discover),
    ):
        # 不 set first_round_done，也能在超时内返回
        result = await asyncio.wait_for(worker._run_background_tasks(), timeout=1)

    assert result == (None, discover, None, None)
