"""后台定时 license 清理任务。

与 openeuler-jenkins 门禁共用同一判定引擎与准入清单（vendored 到
``src/skills/license``），每天扫描全库 skill，删除判定为 failed 的记录：

- 判定状态与门禁一致：allowed / conflict / missing / unknown / restricted / black
- 动作与门禁 ac.yaml 一致：conflict 为 warning（保留），其余 failed 删除
- 仅使用现有 ``SkillRepository.delete`` 删除，不新增表、不改删除口径
- 删除前导出待删数据（skills / skill_versions / security_audits / download_history）
- 结果 JSON 落盘并保留，日志中记录每条删除原因
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from src.core.config import LicenseAuditConfig, get_settings
from src.core.database import AsyncSessionLocal
from src.core.scheduling import next_daily, now_in, parse_hhmm, resolve_timezone
from src.models.orm import (
    DownloadHistory,
    SecurityAudit,
    Skill,
    SkillRepoModel,
    SkillVersion,
)
from src.models.repository import (
    ContributorRepository,
    SkillRepoRepository,
    SkillRepository,
)
from src.skills.discover_result import save_discover_result_file
from src.skills.license.gate_engine import (
    STATUS_BLACK,
    STATUS_ERROR,
    STATUS_MISSING,
    STATUS_RESTRICTED,
    STATUS_UNKNOWN,
    LicenseVerdict,
)
from src.skills.license.runtime import action_for_status, build_checker
from src.skills.license.scanner import (
    LicenseCheckResult,
    derive_skill_dir,
    is_protected_repo,
)

logger = logging.getLogger(__name__)
LOG_PREFIX = "[LicenseAudit]"
RESULT_PREFIX = "license-audit"
_LOCK_KEY = 761124001

_SEVERITY = {
    STATUS_BLACK: 0,
    STATUS_RESTRICTED: 1,
    STATUS_UNKNOWN: 2,
    STATUS_MISSING: 3,
}


class LicenseAuditScheduler:
    """每天固定时刻扫描并清理不合规 skill 的后台任务。"""

    def __init__(self, config: LicenseAuditConfig):
        self._config = config
        self._tz = resolve_timezone(config.timezone)
        self._running = False
        self._task: asyncio.Task[None] | None = None
        # 首轮完成信号：worker 可据此在 LLM 审核前等 license 清理完成
        self.first_round_done = asyncio.Event()

    def _now(self) -> datetime:
        return now_in(self._tz)

    def _check_start_args(self) -> tuple[int, int]:
        return parse_hhmm(self._config.start_time)

    async def start(self) -> None:
        if self._running:
            return
        self._check_start_args()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "%s scheduler started (start_time=%s timezone=%s max_deletions=%d)",
            LOG_PREFIX,
            self._config.start_time,
            self._config.timezone,
            self._config.safety.max_deletions_per_run,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("%s scheduler stopped", LOG_PREFIX)

    async def _loop(self) -> None:
        start = self._check_start_args()
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s round failed", LOG_PREFIX)
            finally:
                self.first_round_done.set()
            if not self._running:
                return
            now = self._now()
            next_run = next_daily(now, start)
            wait_seconds = (next_run - now).total_seconds()
            logger.info(
                "%s next round at %s %s (in %.0fs)",
                LOG_PREFIX,
                next_run.strftime("%Y-%m-%d %H:%M"),
                self._tz.key,
                wait_seconds,
            )
            try:
                await asyncio.sleep(wait_seconds)
            except asyncio.CancelledError:
                return

    # ------------------------------------------------------------------
    # 单轮执行
    # ------------------------------------------------------------------

    async def run_once(self) -> dict[str, Any]:
        """扫描全库并执行删除；返回本轮统计。"""
        run_id = self._now().strftime("%Y%m%d-%H%M%S")
        started = time.monotonic()
        async with AsyncSessionLocal() as lock_session:
            locked = await lock_session.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": _LOCK_KEY}
            )
            if not locked.scalar():
                logger.info("%s skipped: lock held by another task", LOG_PREFIX)
                return {"stopped": "lock_busy"}
            try:
                return await self._run_locked(run_id, started)
            finally:
                await lock_session.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY}
                )

    async def _run_locked(self, run_id: str, started: float) -> dict[str, Any]:
        settings = get_settings()
        config = self._config
        checker = await asyncio.to_thread(
            build_checker,
            settings,
            required=config.safety.abort_on_access_list_failure,
        )
        if checker is None:
            logger.error(
                "%s skipped: license 准入清单不可用且未要求中止（请检查网络/缓存）",
                LOG_PREFIX,
            )
            return {"stopped": "access_list_unavailable"}

        async with AsyncSessionLocal() as session:
            stmt = (
                select(
                    Skill.id,
                    Skill.skill_id,
                    Skill.name,
                    Skill.repo_url,
                    Skill.source_url,
                    Skill.extra_metadata,
                    SkillRepoModel.local_path,
                ).join(SkillRepoModel, Skill.skill_repo_id == SkillRepoModel.id)
            )
            rows = [dict(row._mapping) for row in (await session.execute(stmt)).all()]

        repo_groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            repo_groups.setdefault(row["repo_url"] or "", []).append(row)

        semaphore = asyncio.Semaphore(max(1, config.repo.remote_concurrency))

        async def check_repo(repo_url: str, items: list[dict[str, Any]]):
            async with semaphore:
                results = []
                for item in items:
                    declared = (item.get("extra_metadata") or {}).get("license")
                    skill_dir = derive_skill_dir(item.get("source_url") or "")
                    try:
                        result = await asyncio.to_thread(
                            checker.check,
                            repo_url,
                            item.get("local_path"),
                            skill_dir,
                            declared,
                        )
                    except Exception:
                        logger.exception(
                            "%s license check failed: skill=%s repo=%s",
                            LOG_PREFIX,
                            item["skill_id"],
                            repo_url,
                        )
                        result = LicenseCheckResult(
                            LicenseVerdict(STATUS_ERROR, reasons=["license 检查内部错误"]),
                            "error",
                        )
                    results.append((item, result))
                return results

        checked: list[tuple[dict[str, Any], LicenseCheckResult]] = []
        groups = await asyncio.gather(
            *(check_repo(repo_url, items) for repo_url, items in repo_groups.items())
        )
        for group in groups:
            checked.extend(group)

        by_status: dict[str, int] = {}
        errors: list[dict[str, str]] = []
        source_counts: dict[str, int] = {}
        compliant = 0
        non_compliant = 0
        protected_items: list[dict[str, Any]] = []
        plan: list[tuple[dict[str, Any], Any]] = []
        for item, result in checked:
            verdict: LicenseVerdict = result.verdict
            status = verdict.status
            by_status[status] = by_status.get(status, 0) + 1
            source_counts[result.repo_source] = source_counts.get(result.repo_source, 0) + 1
            if status == STATUS_ERROR or result.repo_source == "error":
                errors.append(
                    {
                        "skill_id": item["skill_id"],
                        "repo_url": item["repo_url"] or "",
                        "reason": "; ".join(verdict.reasons) or "repository unavailable",
                    }
                )
                continue
            action = action_for_status(config, status)
            if action in ("success", "warning"):
                compliant += 1
                continue
            non_compliant += 1
            entry = {
                # id 用于备份查询，结果 JSON 需要可序列化，这里统一转 str
                "id": str(item["id"]),
                "skill_id": item["skill_id"],
                "name": item["name"],
                "repo_url": item["repo_url"],
                "skill_dir": derive_skill_dir(item.get("source_url") or ""),
                "declared_license": (item.get("extra_metadata") or {}).get("license"),
                "status": status,
                "expression": verdict.expression,
                "sources": list(verdict.sources),
                "reasons": list(verdict.reasons),
            }
            if is_protected_repo(item["repo_url"] or "", config.safety.protected_repos):
                entry["protected"] = True
                protected_items.append(entry)
                continue
            plan.append((entry, result))

        plan.sort(
            key=lambda pair: (
                _SEVERITY.get(pair[0]["status"], 9),
                pair[0]["skill_id"],
            )
        )
        batch = plan[: max(0, config.safety.max_deletions_per_run)]
        deleted_items: list[dict[str, Any]] = []
        backup_info: dict[str, Any] | None = None

        if batch and not config.dry_run:
            async with AsyncSessionLocal() as session:
                backup_info = await self._backup_batch(session, batch, run_id)
                deleted_items = await self._delete_batch(session, batch, backup_info)
                if deleted_items:
                    try:
                        await ContributorRepository(session).reconcile_skill_counts()
                        await SkillRepoRepository(session).reconcile_skill_nums()
                        await session.commit()
                    except Exception:
                        await session.rollback()
                        logger.exception("%s failed to reconcile statistics", LOG_PREFIX)

        document = {
            "run_id": run_id,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "dry_run" if config.dry_run else "delete",
            "policy": {
                "engine_commit": config.gate.engine_commit,
                "access_list_hash": checker.access.hash,
                "extra_allow": list(config.gate.extra_allow),
                "prefer_file": config.gate.prefer_file,
                "actions": config.actions.model_dump(),
            },
            "backup": backup_info,
            "counters": {
                "scanned": len(checked),
                "compliant": compliant,
                "non_compliant": non_compliant,
                "protected": len(protected_items),
                "planned": len(plan),
                "deleted": len(deleted_items),
                "remaining": max(0, len(plan) - len(deleted_items)),
                "errors": len(errors),
                "remote_fetched": source_counts.get("remote_fetch", 0),
                "remote_failed": source_counts.get("error", 0),
                "duration_seconds": round(time.monotonic() - started, 1),
            },
            "by_status": by_status,
            "deleted_skills": deleted_items,
            "protected_skills": protected_items,
            "errors": errors,
        }
        self._save_result(document)
        logger.info(
            "%s round finished: scanned=%d compliant=%d non_compliant=%d "
            "protected=%d planned=%d deleted=%d remaining=%d errors=%d duration=%.1fs",
            LOG_PREFIX,
            len(checked),
            compliant,
            non_compliant,
            len(protected_items),
            len(plan),
            len(deleted_items),
            max(0, len(plan) - len(deleted_items)),
            len(errors),
            time.monotonic() - started,
        )
        return document["counters"]

    # ------------------------------------------------------------------
    # 备份与删除
    # ------------------------------------------------------------------

    async def _backup_batch(
        self, session, batch: list[tuple[dict[str, Any], Any]], run_id: str
    ) -> dict[str, Any]:
        """导出本轮待删 skill 相关的四张表数据；失败直接中止本轮。"""
        skill_ids = [uuid.UUID(item[0]["id"]) for item in batch]
        skill_id_strs = [item[0]["skill_id"] for item in batch]

        skill_rows = (
            await session.execute(select(Skill).where(Skill.id.in_(skill_ids)))
        ).scalars().all()
        version_rows = (
            await session.execute(
                select(SkillVersion).where(SkillVersion.skill_id.in_(skill_id_strs))
            )
        ).scalars().all()
        version_ids = [row.id for row in version_rows]
        related_ids = [*skill_ids, *version_ids]

        audit_rows = []
        history_rows = []
        if related_ids:
            audit_rows = (
                await session.execute(
                    select(SecurityAudit).where(
                        SecurityAudit.resource_type == "skill",
                        SecurityAudit.resource_id.in_(related_ids),
                    )
                )
            ).scalars().all()
            history_rows = (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.resource_type == "skill",
                        DownloadHistory.resource_id.in_(related_ids),
                    )
                )
            ).scalars().all()

        payload = {
            "run_id": run_id,
            "tables": {
                "skills": [_row_dict(row) for row in skill_rows],
                "skill_versions": [_row_dict(row) for row in version_rows],
                "security_audits": [_row_dict(row) for row in audit_rows],
                "download_history": [_row_dict(row) for row in history_rows],
            },
        }
        counts = {name: len(rows) for name, rows in payload["tables"].items()}
        expected = {
            "skills": len(skill_ids),
            "skill_versions": len(version_ids),
            "security_audits": len(audit_rows),
            "download_history": len(history_rows),
        }
        if counts != expected:
            raise RuntimeError(
                f"license backup count mismatch: {counts} != {expected}"
            )

        backup_dir = Path(
            self._config.safety.backup_dir
            or str(Path(get_settings().storage.local_path) / "license-backups")
        ).expanduser()
        backup_dir.mkdir(parents=True, exist_ok=True)
        path = backup_dir / f"{run_id}.jsonl.gz"
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        compressed = gzip.compress(raw)
        path.write_bytes(compressed)
        digest = hashlib.sha256(compressed).hexdigest()
        logger.info(
            "%s backup written: %s (%s, %d bytes)",
            LOG_PREFIX, path, counts, len(compressed),
        )
        self._prune_backups(backup_dir)
        return {
            "path": str(path),
            "sha256": digest,
            "size": len(compressed),
            "counts": counts,
        }

    async def _delete_batch(
        self,
        session,
        batch: list[tuple[dict[str, Any], Any]],
        backup_info: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """复用现有 delete()，按批提交并记录删除证据。"""
        repo = SkillRepository(session)
        deleted: list[dict[str, Any]] = []
        # deleted 中已提交的条数；回滚时据此撤销未提交的删除证据
        committed = 0
        batch_size = max(1, self._config.batch_size)
        for index, (entry, _result) in enumerate(batch, start=1):
            try:
                ok = await repo.delete(entry["skill_id"])
            except Exception:
                await session.rollback()
                del deleted[committed:]
                logger.exception(
                    "%s delete failed: skill=%s", LOG_PREFIX, entry["skill_id"]
                )
                continue
            if ok:
                deleted.append(
                    {**entry, "backup_ref": (backup_info or {}).get("path", "")}
                )
                logger.info(
                    "%s delete skill=%s status=%s reason=%s backup=%s",
                    LOG_PREFIX,
                    entry["skill_id"],
                    entry["status"],
                    "; ".join(entry["reasons"]) or "-",
                    (backup_info or {}).get("path", "-"),
                )
            if index % batch_size == 0:
                await session.commit()
                committed = len(deleted)
        await session.commit()
        return deleted

    # ------------------------------------------------------------------
    # 结果文件与备份清理
    # ------------------------------------------------------------------

    def _save_result(self, document: dict[str, Any]) -> Path | None:
        try:
            target_dir = Path(
                self._config.result_dir
                or str(Path(get_settings().storage.local_path) / "logs")
            ).expanduser()
            path = save_discover_result_file(
                document,
                result_dir=str(target_dir),
                file_prefix=RESULT_PREFIX,
                log=False,
            )
            if path is not None:
                self._prune_results(
                    path.parent,
                    self._config.result_keep_days,
                    self._config.result_max_files,
                )
            return path
        except Exception:
            logger.warning("%s failed to save result file", LOG_PREFIX, exc_info=True)
            return None

    @staticmethod
    def _prune_results(directory: Path, keep_days: int, max_files: int) -> int:
        try:
            files = sorted(
                directory.glob(f"{RESULT_PREFIX}-*.json"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return 0
        cutoff = time.time() - keep_days * 86400 if keep_days > 0 else None
        removed = 0
        for index, path in enumerate(files):
            try:
                too_many = max_files > 0 and index >= max_files
                too_old = cutoff is not None and path.stat().st_mtime < cutoff
                if too_many or too_old:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def _prune_backups(self, directory: Path) -> None:
        keep_days = self._config.safety.backup_keep_days
        max_files = self._config.safety.backup_keep_files
        try:
            files = sorted(
                directory.glob("*.jsonl.gz"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        cutoff = time.time() - keep_days * 86400 if keep_days > 0 else None
        for index, path in enumerate(files):
            try:
                too_many = max_files > 0 and index >= max_files
                too_old = cutoff is not None and path.stat().st_mtime < cutoff
                if too_many or too_old:
                    path.unlink()
            except OSError:
                continue


def _row_dict(row) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, uuid.UUID):
            value = str(value)
        data[column.name] = value
    return data


async def start_license_audit_scheduler() -> LicenseAuditScheduler | None:
    """按配置创建并启动 license 清理调度器；未启用时返回 None。"""
    settings = get_settings()
    config = settings.license_audit
    if not config.enabled:
        logger.info("%s disabled (license_audit.enabled=false)", LOG_PREFIX)
        return None
    scheduler = LicenseAuditScheduler(config)
    await scheduler.start()
    return scheduler
