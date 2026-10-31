"""统一补齐编排（src/jobs/remediation.py）与安全补跑
（src/ai/security_backfill.py）的测试。"""

import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai import security_backfill
from src.ai.security_backfill import (
    LockBusyError,
    SecurityBackfillStats,
    _UnscoredRow,
    backfill_missing_security_audits,
)
from src.jobs import remediation
from src.jobs.remediation import run_remediations


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def lock_session_with(locked: bool) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar.return_value = locked
    session.execute.return_value = result
    return session


def row(table: str = "skills") -> _UnscoredRow:
    return _UnscoredRow(
        table=table,
        id=uuid.uuid4(),
        skill_id="github/acme/agent-skills/example",
        source_url="https://github.com/acme/agent-skills/blob/main/SKILL.md",
        version=None,
        commit_id=None,
        repo_url="https://github.com/acme/agent-skills",
        repo_branch=None,
        repo_source="github",
        repo_local_path=None,
    )


class TestRunRemediations:
    async def test_runs_all_tasks_in_order(self):
        """security → category → embedding 固定顺序执行并收集各任务统计。"""
        order: list[str] = []

        def make_runner(name, stats):
            async def runner():
                order.append(name)
                return stats

            return runner

        with (
            patch.object(remediation, "_run_security", make_runner("security", {"a": 1})),
            patch.object(remediation, "_run_category", make_runner("category", {"b": 2})),
            patch.object(remediation, "_run_embedding", make_runner("embedding", {"c": 3})),
        ):
            results = await run_remediations()

        assert order == ["security", "category", "embedding"]
        assert results == {
            "security": {"a": 1},
            "category": {"b": 2},
            "embedding": {"c": 3},
        }

    async def test_task_failure_is_isolated(self):
        """单任务异常记入该任务统计，不影响后续任务执行。"""

        async def broken():
            raise RuntimeError("boom")

        async def ok():
            return {"filled": 1}

        with (
            patch.object(remediation, "_run_security", broken),
            patch.object(remediation, "_run_category", ok),
            patch.object(remediation, "_run_embedding", ok),
        ):
            results = await run_remediations()

        assert results["security"] == {"error": "remediation task failed"}
        assert results["category"] == {"filled": 1}
        assert results["embedding"] == {"filled": 1}

    def test_save_remediation_result_file_writes_document(self, tmp_path):
        """每轮补齐结果写 remediation-<时间戳>.json，统计字段带 <key>_note。"""
        started = datetime(2026, 10, 29, 8, 0, 0)

        with patch(
            "src.skills.discover_result.resolve_discover_result_dir",
            return_value=tmp_path,
        ):
            remediation._save_remediation_result_file(
                {"security": {"total": 2, "triggered": 2, "failed": 0, "skipped": None}},
                started,
                run_started=100.0,
            )

        files = list(tmp_path.glob("remediation-*.json"))
        assert len(files) == 1
        document = json.loads(files[0].read_text(encoding="utf-8"))
        assert document["started_at"] == "2026-10-29T08:00:00"
        assert "total_seconds" in document
        security = document["remediation_run"]["security"]
        assert security["total"] == 2
        assert security["total_note"]
        assert security["skipped"] is None


class TestSecurityBackfill:
    async def test_raises_lock_busy_and_does_not_unlock(self, monkeypatch):
        lock_session = lock_session_with(False)
        monkeypatch.setattr(
            security_backfill,
            "AsyncSessionLocal",
            lambda: AsyncContext(lock_session),
        )

        with pytest.raises(LockBusyError):
            await backfill_missing_security_audits()

        # 只有 try_lock 一次，未持有锁就不解锁
        assert lock_session.execute.await_count == 1

    async def test_skips_when_audit_disabled(self):
        """安全检测关闭时直接跳过，不触达数据库。"""
        settings = MagicMock()
        settings.security.enable_audit = False
        with patch.object(security_backfill, "get_settings", return_value=settings):
            stats = await backfill_missing_security_audits()

        assert stats.skipped == "security audit disabled"
        assert stats.total == 0

    async def test_skips_when_skillspector_unavailable(self, monkeypatch):
        lock_session = lock_session_with(True)
        monkeypatch.setattr(
            security_backfill,
            "AsyncSessionLocal",
            lambda: AsyncContext(lock_session),
        )
        settings = MagicMock()
        settings.security.enable_audit = True

        with (
            patch.object(security_backfill, "get_settings", return_value=settings),
            patch.object(security_backfill, "_build_security_tools", return_value=(None, None)),
        ):
            stats = await backfill_missing_security_audits()

        assert stats.skipped == "skillspector unavailable"

    async def test_covers_both_record_tables(self, monkeypatch):
        """latest Skill 与 Tag 版本两张表都扫；游标逐批推进到空批次。"""
        lock_session = lock_session_with(True)
        monkeypatch.setattr(
            security_backfill,
            "AsyncSessionLocal",
            lambda: AsyncContext(lock_session),
        )
        settings = MagicMock()
        settings.security.enable_audit = True

        skills_rows = [row("skills")]
        versions_rows = [row("skill_versions")]

        async def fetch(table, after_id, limit):
            if table == "skills":
                return skills_rows if after_id is None else []
            return versions_rows if after_id is None else []

        write_mock = AsyncMock(return_value=1)
        with (
            patch.object(security_backfill, "get_settings", return_value=settings),
            patch.object(
                security_backfill, "_build_security_tools", return_value=(MagicMock(), MagicMock())
            ),
            patch.object(security_backfill, "_fetch_gap_chunk", AsyncMock(side_effect=fetch)),
            patch.object(security_backfill, "_trigger_chunk", AsyncMock(side_effect=lambda rows, *args: [(r, {"skillspector_build_number": 1}) for r in rows])),
            patch.object(security_backfill, "_write_chunk", write_mock),
        ):
            stats = await backfill_missing_security_audits(chunk_size=1)

        assert stats == SecurityBackfillStats(total=2, triggered=2, failed=0)
        assert write_mock.await_count == 2
        # 锁 + 解锁各一次
        assert lock_session.execute.await_count == 2
