"""DiscoverScheduler 单元测试：时间计算、配置接入与调度循环。"""

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.jobs.discover_scheduler import DiscoverScheduler
from src.core.config import DiscoverSchedulerConfig
from src.core.scheduling import next_daily


class TestNextDaily:
    def test_before_trigger_time_today(self):
        now = datetime(2026, 8, 28, 1, 30)
        nxt = next_daily(now, (3, 0))
        assert nxt == datetime(2026, 8, 28, 3, 0)

    def test_after_trigger_time_rolls_to_tomorrow(self):
        now = datetime(2026, 8, 28, 4, 0)
        nxt = next_daily(now, (3, 0))
        assert nxt == datetime(2026, 8, 29, 3, 0)

    def test_at_exact_trigger_time_rolls_to_tomorrow(self):
        now = datetime(2026, 8, 28, 3, 0, 0, 1)
        nxt = next_daily(now, (3, 0))
        assert nxt == datetime(2026, 8, 29, 3, 0)


class TestConfigIntegration:
    def test_settings_has_discover_scheduler_defaults(self):
        from src.core.config import Settings

        settings = Settings()
        assert settings.discover_scheduler.enabled is False
        assert settings.discover_scheduler.start_time == "03:00"
        assert settings.discover_scheduler.timezone == "Asia/Shanghai"

    def test_config_yaml_section_parsed(self):
        # 验证 from_yaml 的解析路径；不绑定 config.yaml 的具体取值
        # （调度时刻等运维随时可改，断言具体值会随配置漂移误报）
        import re

        from src.core.config import Settings

        settings = Settings.from_yaml("config.yaml")
        assert isinstance(settings.discover_scheduler.enabled, bool)
        assert re.fullmatch(r"\d{2}:\d{2}", settings.discover_scheduler.start_time)
        assert settings.discover_scheduler.timezone

    def test_invalid_start_time_rejected_on_start(self):
        config = DiscoverSchedulerConfig(enabled=True, start_time="25:00")
        scheduler = DiscoverScheduler(config)
        with pytest.raises(ValueError, match="HH:MM"):
            import asyncio

            asyncio.run(scheduler.start())


def _fake_result(**flags):
    return SimpleNamespace(skill_num=2, url="https://atomgit.com/a/b", **flags)


@pytest.fixture(autouse=True)
def _isolate_remediation():
    """统一屏蔽补跑任务：避免测试触达 Jenkins / DB / embedding 服务。

    需要验证补跑口径的用例自行 patch 同一目标即可覆盖此默认值。
    """
    with patch(
        "src.jobs.remediation.run_remediations",
        AsyncMock(return_value={}),
    ):
        yield


class TestRunDiscoverOnce:
    @pytest.fixture
    def requests(self):
        return (
            [
                SimpleNamespace(url="https://atomgit.com/a/b"),
                SimpleNamespace(url="https://atomgit.com/c/d"),
                SimpleNamespace(url="https://atomgit.com/e/f"),
            ],
            ["community"],
        )

    async def test_classifies_each_repo_and_logs_summary(self, requests, caplog, tmp_path):
        scheduler = DiscoverScheduler(
            DiscoverSchedulerConfig(enabled=True, result_dir=str(tmp_path))
        )
        caplog.set_level(logging.INFO, logger="src.jobs.discover_scheduler")

        results = [
            _fake_result(_created_new=True),
            _fake_result(_unchanged=True),
            _fake_result(_removed_existing=True),
        ]

        with (
            patch(
                "src.jobs.discover_scheduler.build_configured_discover_requests",
                return_value=requests,
            ),
            patch(
                "src.jobs.discover_scheduler.AsyncSessionLocal"
            ) as session_factory,
        ):
            session_ctx = AsyncMock()
            session_factory.return_value = session_ctx
            manager = SimpleNamespace(
                discover_configured_skill_repository=AsyncMock(side_effect=results)
            )
            with patch(
                "src.jobs.discover_scheduler.SkillManager", return_value=manager
            ):
                counters = await scheduler.run_discover_once()

        assert counters == {
            "created": 1,
            "rediscovered": 0,
            "unchanged": 1,
            "removed": 1,
            "no_skill": 0,
            "failed": 0,
        }

        # 每轮开始/结束日志与每仓耗时日志均已输出
        assert "round started" in caplog.text
        assert "round finished" in caplog.text
        assert "total" in caplog.text
        assert "avg" in caplog.text
        assert "slowest" in caplog.text
        assert caplog.text.count("took") == 3

    async def test_result_file_saved_with_details(self, requests, tmp_path):
        """每轮结果写入 JSON 文件，包含汇总统计与每仓明细。"""
        import json as json_lib

        scheduler = DiscoverScheduler(
            DiscoverSchedulerConfig(enabled=True, result_dir=str(tmp_path))
        )
        results = [
            _fake_result(_created_new=True),
            _fake_result(_unchanged=True),
            _fake_result(_removed_existing=True),
        ]
        remediation_run = {
            "security": {"total": 2, "triggered": 2, "failed": 0, "skipped": None},
            "category": {"total": 0, "filled": 0, "unclassified": 0, "failed": 0, "skipped": None},
            "embedding": {
                "total": 1,
                "indexed": 1,
                "failed_batches": 0,
                "failed_rows": 0,
                "skipped_empty": 0,
                "stale_skipped": 0,
            },
        }

        with (
            patch(
                "src.jobs.discover_scheduler.build_configured_discover_requests",
                return_value=requests,
            ),
            patch(
                "src.jobs.discover_scheduler.AsyncSessionLocal"
            ) as session_factory,
            patch(
                "src.jobs.remediation.run_remediations",
                AsyncMock(return_value=remediation_run),
            ),
        ):
            session_factory.return_value = AsyncMock()
            manager = SimpleNamespace(
                discover_configured_skill_repository=AsyncMock(side_effect=results)
            )
            with patch(
                "src.jobs.discover_scheduler.SkillManager", return_value=manager
            ):
                await scheduler.run_discover_once()

        files = list(tmp_path.glob("discover-*.json"))
        assert len(files) == 1
        document = json_lib.loads(files[0].read_text(encoding="utf-8"))

        assert document["total_repos"] == 3
        assert document["counters"] == {
            "created": 1,
            "created_note": "新建仓库记录并完成扫描",
            "rediscovered": 0,
            "rediscovered_note": "已有仓库 commit 变化并完成重扫",
            "unchanged": 1,
            "unchanged_note": "HEAD commit 与数据库相同，跳过扫描",
            "removed": 1,
            "removed_note": "已有仓库不再包含可扫描 Skill，删除 DB 仓库记录",
            "no_skill": 0,
            "no_skill_note": "新仓库没有可扫描 Skill，不创建 DB 仓库记录",
            "failed": 0,
            "failed_note": "当前仓库处理失败（clone/fetch 或扫描异常），详见 repos[].error",
        }
        assert document["config_keys"] == ["community"]
        # 各区块每项为 <key> + <key>_note（统计查询失败时 skill_stats 为 null）
        assert document["skill_stats"] is None
        # remediation_run 三个任务各自带 <key>_note
        run_doc = document["remediation_run"]
        assert run_doc["security"]["triggered"] == 2
        assert run_doc["security"]["triggered_note"] == "本轮成功重新触发 Jenkins 检测的行数"
        assert run_doc["category"]["filled"] == 0
        assert run_doc["category"]["filled_note"] == "本轮成功写入分类的行数"
        embed_run = run_doc["embedding"]
        assert embed_run["indexed"] == 1
        assert embed_run["indexed_note"] == "本轮成功写入 embedding 的行数"
        assert embed_run["total_note"] == "本轮扫描到的待编码行数"
        for task_stats in run_doc.values():
            assert all(
                task_stats[f"{key}_note"]
                for key in task_stats
                if not key.endswith("_note")
            )
        assert "started_at" in document
        assert "finished_at" in document
        assert "total_seconds" in document
        assert "avg_seconds_per_repo" in document
        assert len(document["slowest_repos"]) == 3

        by_url = {repo["url"]: repo for repo in document["repos"]}
        assert len(by_url) == 3
        created = by_url["https://atomgit.com/a/b"]
        assert created["outcome"] == "created"
        assert created["skill_num"] == 2
        assert "duration_seconds" in created
        assert "error" not in created

    async def test_failed_repo_error_message_saved(self, requests, tmp_path):
        """失败仓库的异常信息写入结果文件。"""
        import json as json_lib

        scheduler = DiscoverScheduler(
            DiscoverSchedulerConfig(enabled=True, result_dir=str(tmp_path))
        )
        with (
            patch(
                "src.jobs.discover_scheduler.build_configured_discover_requests",
                return_value=requests,
            ),
            patch(
                "src.jobs.discover_scheduler.AsyncSessionLocal"
            ) as session_factory,
        ):
            session_factory.return_value = AsyncMock()
            manager = SimpleNamespace(
                discover_configured_skill_repository=AsyncMock(
                    side_effect=[RuntimeError("clone timeout"), None, None]
                )
            )
            with patch(
                "src.jobs.discover_scheduler.SkillManager", return_value=manager
            ):
                await scheduler.run_discover_once()

        document = json_lib.loads(
            next(tmp_path.glob("discover-*.json")).read_text(encoding="utf-8")
        )
        failed = next(repo for repo in document["repos"] if repo["outcome"] == "failed")
        assert failed["error"] == "clone timeout"

    async def test_result_save_failure_does_not_break_run(self, requests, caplog):
        """结果文件写失败（目录不可写）不影响本轮执行与返回值。"""
        scheduler = DiscoverScheduler(
            DiscoverSchedulerConfig(enabled=True, result_dir="/proc/forbidden-dir")
        )
        caplog.set_level(logging.WARNING, logger="src.jobs.discover_scheduler")

        with (
            patch(
                "src.jobs.discover_scheduler.build_configured_discover_requests",
                return_value=requests,
            ),
            patch(
                "src.jobs.discover_scheduler.AsyncSessionLocal"
            ) as session_factory,
        ):
            session_factory.return_value = AsyncMock()
            manager = SimpleNamespace(
                discover_configured_skill_repository=AsyncMock(
                    side_effect=[
                        _fake_result(_created_new=True),
                        _fake_result(_unchanged=True),
                        _fake_result(_removed_existing=True),
                    ]
                )
            )
            with patch(
                "src.jobs.discover_scheduler.SkillManager", return_value=manager
            ):
                counters = await scheduler.run_discover_once()

        assert counters["created"] == 1
        assert "Failed to save discover result file" in caplog.text

    async def test_single_repo_failure_does_not_abort_run(self, requests, caplog, tmp_path):
        scheduler = DiscoverScheduler(
            DiscoverSchedulerConfig(enabled=True, result_dir=str(tmp_path))
        )
        caplog.set_level(logging.INFO, logger="src.jobs.discover_scheduler")

        with (
            patch(
                "src.jobs.discover_scheduler.build_configured_discover_requests",
                return_value=requests,
            ),
            patch(
                "src.jobs.discover_scheduler.AsyncSessionLocal"
            ) as session_factory,
        ):
            session_factory.return_value = AsyncMock()
            manager = SimpleNamespace(
                discover_configured_skill_repository=AsyncMock(
                    side_effect=[_fake_result(_created_new=True), RuntimeError("boom"), None]
                )
            )
            with patch(
                "src.jobs.discover_scheduler.SkillManager", return_value=manager
            ):
                counters = await scheduler.run_discover_once()

        assert counters["created"] == 1
        assert counters["failed"] == 1
        assert counters["no_skill"] == 1

        # 失败仓库的日志带耗时（after Xs）
        assert "[Discover] failed for" in caplog.text
        assert "after" in caplog.text


class TestSchedulerLoop:
    async def test_loop_runs_immediately_then_at_scheduled_time(self):
        """启动即跑一轮，之后睡眠到调度点再跑；run 完才计算下一个触发点。"""
        config = DiscoverSchedulerConfig(enabled=True, start_time="03:00")
        scheduler = DiscoverScheduler(config)

        calls = []

        async def fake_sleep(seconds: float):
            # 记录计算出的等待间隔后立即“到点”
            calls.append(seconds)

        async def fake_run(sched):
            calls.append("run")
            if calls.count("run") == 2:  # 第二轮执行后停止循环
                sched._running = False

        fixed_now = datetime(2026, 8, 28, 1, 0)
        with (
            patch("src.jobs.discover_scheduler.asyncio.sleep", fake_sleep),
            patch.object(DiscoverScheduler, "_now", new=lambda self: fixed_now),
            patch.object(DiscoverScheduler, "run_discover_once", fake_run),
        ):
            scheduler._running = True  # 直接驱动 _loop（不经 start() 创建 task）
            await scheduler._loop()

        # 启动先跑一轮；1:00 -> 3:00 间隔 2 小时；到点再跑第二轮
        assert calls == ["run", timedelta(hours=2).total_seconds(), "run"]

    async def test_start_stop_lifecycle(self):
        config = DiscoverSchedulerConfig(enabled=True)
        scheduler = DiscoverScheduler(config)
        with patch.object(DiscoverScheduler, "run_discover_once", AsyncMock()):
            await scheduler.start()
            assert scheduler._running is True
            assert scheduler._task is not None
            await scheduler.stop()
        assert scheduler._running is False
        assert scheduler._task is None

    async def test_start_is_idempotent(self):
        config = DiscoverSchedulerConfig(enabled=True)
        scheduler = DiscoverScheduler(config)
        with patch.object(DiscoverScheduler, "run_discover_once", AsyncMock()):
            await scheduler.start()
            task = scheduler._task
            await scheduler.start()
            assert scheduler._task is task
            await scheduler.stop()


class TestStartDiscoverScheduler:
    async def test_returns_none_when_disabled(self):
        from src.jobs.discover_scheduler import start_discover_scheduler

        settings = SimpleNamespace(
            discover_scheduler=DiscoverSchedulerConfig(enabled=False)
        )
        with patch("src.core.config.get_settings", return_value=settings):
            result = await start_discover_scheduler()
        assert result is None

    async def test_starts_and_returns_scheduler_when_enabled(self):
        from src.jobs.discover_scheduler import start_discover_scheduler

        settings = SimpleNamespace(
            discover_scheduler=DiscoverSchedulerConfig(enabled=True)
        )
        with patch("src.core.config.get_settings", return_value=settings):
            scheduler = await start_discover_scheduler()
        assert scheduler is not None
        await scheduler.stop()


class TestSkillStatsWithNotes:
    def test_nested_sections_get_notes(self):
        """skill_stats 各层字段（含 security_audit / index_creation）均补 <key>_note。"""
        from src.skills.discover_result import skill_stats_with_notes

        raw = {
            "total_skills": 9008,
            "category_distribution": {"others": 12},
            "security_audit": {
                "audited": 8996,
                "pending": 0,
                "failed": 0,
                "failed_skills": [],
                "failed_skills_truncated": False,
            },
            "index_creation": {
                "total": 9008,
                "indexed": 9000,
                "missing": 0,
                "not_required": 8,
            },
        }
        merged = skill_stats_with_notes(raw)

        # category_distribution 是数据字典（{category: count}），不参与递归说明断言
        data_only_keys = {"category_distribution"}

        def assert_every_key_noted(node):
            for key, value in node.items():
                if key.endswith("_note") or key in data_only_keys:
                    continue
                if isinstance(value, dict):
                    assert node.get(f"{key}_note"), f"{key} 缺说明"
                    assert_every_key_noted(value)
                else:
                    assert node.get(f"{key}_note"), f"{key} 缺说明"

        assert_every_key_noted(merged)
        # 数值原样保留，说明挂在对应 <key>_note 上
        assert merged["total_skills"] == 9008
        assert merged["security_audit"]["audited"] == 8996
        assert merged["index_creation"]["not_required"] == 8
        assert merged["index_creation"]["not_required_note"].startswith("未审计且未建索引")
