"""安全检测补跑：为 risk_score 缺口行重新触发 Jenkins 安全检测。

自愈缺口：审计触发失败（Jenkins 不可达等）的行 risk_score 为空，内容不变时
reconcile 不重扫，缺口不会自行消失；本任务全局扫描缺口行（不限于某轮 discover
扫到的仓库）并重新触发检测，评分由 SkillspectorCollector 异步回填。

缺口口径：``risk_score IS NULL`` 且无在途异步审计（有 build number 且
``skillspector_collected`` 为空的行由 collector 轮询收尾，本轮不重复触发）。

并发安全：pg_advisory_lock 与其它补齐任务互斥，锁被占用时抛 LockBusyError
由调用方决定跳过；写库走 ``upsert_by_resource``（与扫描路径同表同口径），
extra_metadata.security_audit 用 jsonb_set 条件更新审计子键，永不覆盖其它元数据。

批次短事务（与 category_backfill 同款三段式，Jenkins 触发是 HTTP 调用、
期间不占 DB 连接）：
  ① 短会话 keyset 读一批缺口行 → 关闭
  ② 无 DB 连接逐条触发审计（与扫描路径共用 src.security.audit_trigger）
  ③ 短会话按 id 排序写入 → commit → 关闭
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text

from src.core.config import get_settings
from src.core.database import AsyncSessionLocal
from src.models.repository import SecurityAuditRepository
from src.security.audit_trigger import trigger_existing_skill_audit
from src.security.detector import SecurityDetector
from src.utils.git_operations import GitOperations
from src.skills.skill_id import resolve_relative_skill_path

logger = logging.getLogger(__name__)
LOG_PREFIX = "[Remediation][Security]"

# 安全补齐的 advisory lock key（embedding ...122 / category ...123 之外第三个）
_SECURITY_LOCK_KEY = 742_301_990_124

# 单批行数：批内逐条串行触发 Jenkins，批大小只影响一次事务的写入行数
CHUNK_SIZE = 32

# 缺口查询覆盖的两张记录表（latest Skill 与 Tag 版本），键仅作字面量拼接
_RECORD_TABLES = ("skills", "skill_versions")


class LockBusyError(RuntimeError):
    """另一个安全补齐任务正持有锁。"""


@dataclass
class SecurityBackfillStats:
    """一轮安全补齐任务的统计。"""

    total: int = 0          # 扫描到的缺口行数（risk_score 为空且无在途审计）
    triggered: int = 0      # 成功重新触发 Jenkins 检测的行数
    failed: int = 0         # 触发失败（未获得 build number）的行数
    skipped: str | None = None  # 本轮跳过原因（安全检测关闭 / skillspector 不可用）

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "triggered": self.triggered,
            "failed": self.failed,
            "skipped": self.skipped,
        }


@dataclass(frozen=True)
class _UnscoredRow:
    """一批缺口读取快照中的一行待补审计记录。"""

    table: str
    id: uuid.UUID
    skill_id: str
    source_url: str
    version: str | None
    commit_id: str | None
    repo_url: str | None
    repo_branch: str | None
    repo_source: str
    repo_local_path: str | None

    def as_record(self) -> SimpleNamespace:
        """适配 resolve_relative_skill_path 的记录视图。"""
        return SimpleNamespace(
            skill_id=self.skill_id,
            source_url=self.source_url,
            version=self.version,
            commit_id=self.commit_id,
        )

    def as_repo(self) -> SimpleNamespace:
        """适配 resolve_relative_skill_path / trigger_existing_skill_audit 的仓库视图。"""
        return SimpleNamespace(
            url=self.repo_url,
            branch=self.repo_branch,
            source=self.repo_source,
            local_path=self.repo_local_path,
        )


# extra_metadata.security_audit 的条件回写：jsonb_set 只动审计子键，
# 不做读改写，并发安全；表名字面量来自 _RECORD_TABLES 常量。
_METADATA_UPDATE_SQL = {
    table: text(
        f"UPDATE {table} SET extra_metadata = jsonb_set("
        "coalesce(extra_metadata, '{}'::jsonb), '{security_audit}', "
        "CAST(:details AS jsonb)) WHERE id = :id"
    )
    for table in _RECORD_TABLES
}


async def _fetch_gap_chunk(
    table: str, after_id: uuid.UUID | None, limit: int
) -> list[_UnscoredRow]:
    """keyset 分页读取一张表的缺口行（短会话）。"""
    cursor_cond = "AND s.id > :after_id" if after_id is not None else ""
    sql = text(
        f"""
        SELECT s.id, s.skill_id, s.source_url, s.version, s.commit_id,
               r.url, r.branch, r.source, r.local_path
        FROM {table} s
        JOIN skill_repos r ON r.id = s.skill_repo_id
        WHERE s.risk_score IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM security_audits a
              WHERE a.resource_type = 'skill' AND a.resource_id = s.id
                AND a.details ->> 'skillspector_build_number' IS NOT NULL
                AND a.details ->> 'skillspector_collected' IS NULL
          )
          {cursor_cond}
        ORDER BY s.id
        LIMIT :limit
        """
    )
    params: dict[str, Any] = {"limit": limit}
    if after_id is not None:
        params["after_id"] = after_id
    async with AsyncSessionLocal() as session:
        result = await session.execute(sql, params)
        rows = result.all()
    return [
        _UnscoredRow(
            table=table,
            id=row[0],
            skill_id=row[1] or "",
            source_url=row[2] or "",
            version=row[3],
            commit_id=row[4],
            repo_url=row[5],
            repo_branch=row[6],
            repo_source=row[7] or "",
            repo_local_path=row[8],
        )
        for row in rows
    ]


async def _trigger_chunk(
    rows: list[_UnscoredRow],
    git_ops: GitOperations,
    detector: SecurityDetector,
    stats: SecurityBackfillStats,
) -> list[tuple[_UnscoredRow, dict[str, Any]]]:
    """逐条触发 Jenkins 异步审计（HTTP 调用放线程池），返回成功行与其 details。

    与扫描路径共用 ``src.security.audit_trigger.trigger_existing_skill_audit``
    （物化 blob、async 触发、失败降级口径一致）；触发失败（无 build number）
    的行只计数不写库，留待下一轮重试。
    """
    triggered: list[tuple[_UnscoredRow, dict[str, Any]]] = []
    for row in rows:
        repo = row.as_repo()
        record = row.as_record()
        try:
            relative_path = resolve_relative_skill_path(repo, record)
            report = await trigger_existing_skill_audit(
                git_ops=git_ops,
                security_detector=detector,
                security_async_mode=True,
                repo=repo,
                relative_path=relative_path,
                commit_id=row.commit_id,
                skill_id=row.skill_id,
            )
        except Exception:
            stats.failed += 1
            logger.exception("%s trigger exception: skill_id=%s", LOG_PREFIX, row.skill_id)
            continue
        details = dict(report.details)
        if details.get("skillspector_build_number") is None:
            stats.failed += 1
            logger.warning(
                "%s trigger failed (no build number): skill_id=%s version=%s",
                LOG_PREFIX,
                row.skill_id,
                row.version or "-",
            )
            continue
        triggered.append((row, details))
    return triggered


async def _write_chunk(triggered: list[tuple[_UnscoredRow, dict[str, Any]]]) -> int:
    """写入审计记录与元数据（短会话单事务）。按 id 排序写消除并发死锁。

    审计行经 ``upsert_by_resource`` 写入（与扫描路径同表同口径），
    extra_metadata.security_audit 用 jsonb_set 只更新审计子键。
    """
    written = 0
    async with AsyncSessionLocal() as session:
        audit_repo = SecurityAuditRepository(session)
        for row, details in sorted(triggered, key=lambda pair: pair[0].id):
            await audit_repo.upsert_by_resource(
                resource_type="skill",
                resource_id=row.id,
                audit_data={
                    "resource_type": "skill",
                    "resource_id": row.id,
                    "version": row.version,
                    "commit_id": row.commit_id,
                    "audit_type": "skillspector",
                    "risk_level": "unknown",
                    "risk_signals": [],
                    "details": details,
                    "audited_at": datetime.now(timezone.utc),
                },
            )
            await session.execute(
                _METADATA_UPDATE_SQL[row.table],
                {"id": row.id, "details": json.dumps(details, ensure_ascii=False)},
            )
            written += 1
        await session.commit()
    return written


def _build_security_tools() -> tuple[GitOperations, SecurityDetector | None]:
    """初始化 git 操作与安全检测器；检测器不可用返回 (GitOperations, None) 由调用方跳过。"""
    detector = SecurityDetector()
    return GitOperations(), detector if detector.has_skillspector else None


async def backfill_missing_security_audits(
    chunk_size: int = CHUNK_SIZE,
) -> SecurityBackfillStats:
    """为 risk_score 缺口行重新触发 Jenkins 安全检测。

    锁被另一个补齐任务持有时抛 LockBusyError，由调用方决定跳过。
    """
    stats = SecurityBackfillStats()
    if not get_settings().security.enable_audit:
        stats.skipped = "security audit disabled"
        logger.info("%s skipped: security audit disabled", LOG_PREFIX)
        return stats

    logger.info("%s backfill started", LOG_PREFIX)
    async with AsyncSessionLocal() as lock_session:
        locked = await lock_session.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _SECURITY_LOCK_KEY}
        )
        if not locked.scalar():
            raise LockBusyError("security backfill lock is held by another task")
        try:
            git_ops, detector = await asyncio.to_thread(_build_security_tools)
            if detector is None:
                stats.skipped = "skillspector unavailable"
                logger.warning("%s skipped: skillspector unavailable", LOG_PREFIX)
                return stats

            for table in _RECORD_TABLES:
                after_id: uuid.UUID | None = None
                while True:
                    rows = await _fetch_gap_chunk(table, after_id, chunk_size)
                    if not rows:
                        break
                    # 无论触发结果如何都推进游标，避免反复读取同一批。
                    after_id = rows[-1].id
                    stats.total += len(rows)
                    logger.info(
                        "%s chunk: table=%s gap_rows=%d cursor=%s",
                        LOG_PREFIX, table, len(rows), after_id,
                    )

                    triggered = await _trigger_chunk(rows, git_ops, detector, stats)
                    if triggered:
                        await _write_chunk(triggered)
                        stats.triggered += len(triggered)

                    if len(rows) < chunk_size:
                        break
            logger.info(
                "%s backfill finished: total=%d triggered=%d failed=%d",
                LOG_PREFIX, stats.total, stats.triggered, stats.failed,
            )
            return stats
        finally:
            await lock_session.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _SECURITY_LOCK_KEY}
            )
