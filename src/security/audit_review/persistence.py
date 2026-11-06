"""Persist review results and failure/skip state for one audit."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select, update

from src.core.database import AsyncSessionLocal
from src.models.orm import SecurityAudit, Skill, SkillVersion
from src.security.audit_review.analysis import _coerce_int
from src.security.audit_review.apply import ReviewApplication, _merge_context_meta
from src.security.audit_review.constants import PROMPT_VERSION
from src.security.audit_review.context import CONTEXT_VERSION
from src.security.audit_review.log import get_logger
from src.core.scheduling import now_in, resolve_timezone

logger = get_logger("persistence")


def report_fingerprint(report: dict[str, Any]) -> str:
    """报告指纹：用于检测复核期间是否发生了重新扫描。"""
    raw_metadata = report.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    raw_skill = report.get("skill")
    skill: dict[str, Any] = raw_skill if isinstance(raw_skill, dict) else {}
    return str(metadata.get("scanned_at") or skill.get("scanned_at") or "")


async def persist(
    row: dict[str, Any],
    reviewed_report: dict[str, Any],
    application: ReviewApplication,
) -> str:
    audit_id = row.get("id")
    expected_fingerprint = report_fingerprint(reviewed_report)
    async with AsyncSessionLocal() as session:
        current = (
            await session.execute(
                select(SecurityAudit.details).where(SecurityAudit.id == audit_id).with_for_update()
            )
        ).first()
        if current is None:
            logger.info("audit %s disappeared before persistence", audit_id)
            return "skipped"
        current_details = copy.deepcopy(current[0]) if isinstance(current[0], dict) else {}
        current_report = current_details.get("skillspector_report")
        if not isinstance(current_report, dict):
            current_report = {}
        if report_fingerprint(current_report) != expected_fingerprint:
            logger.info("audit %s was re-scanned during review; result discarded", audit_id)
            return "skipped"

        # 基于当前最新 details 合并本次复核结果，避免覆盖并发写入的其他字段
        updated_details = copy.deepcopy(current_details)
        for key in (
            "skillspector_report",
            "skillspector_score",
            "recommendation",
            "llm_review",
        ):
            if key in application.details:
                updated_details[key] = application.details[key]
        if "risk_level" in updated_details:
            updated_details["risk_level"] = application.risk_level
        if "risk_signals" in updated_details:
            updated_details["risk_signals"] = application.risk_signals

        await session.execute(
            update(SecurityAudit)
            .where(SecurityAudit.id == audit_id)
            .values(
                risk_level=application.risk_level,
                risk_signals=application.risk_signals,
                details=updated_details,
            )
        )
        if application.score is not None:
            resource_id = row.get("resource_id")
            updated = await session.execute(
                update(Skill).where(Skill.id == resource_id).values(risk_score=application.score)
            )
            if updated.rowcount == 0:
                await session.execute(
                    update(SkillVersion)
                    .where(SkillVersion.id == resource_id)
                    .values(risk_score=application.score)
                )
        await session.commit()
        return "reviewed"


async def persist_partial(
    audit_id: Any,
    reviewed_report: dict[str, Any],
    partial_meta: dict[str, Any],
) -> str:
    """保存分批续跑游标（不修改风险项、分数与等级）。"""
    expected_fingerprint = report_fingerprint(reviewed_report)
    try:
        async with AsyncSessionLocal() as session:
            current = (
                await session.execute(
                    select(SecurityAudit.details)
                    .where(SecurityAudit.id == audit_id)
                    .with_for_update()
                )
            ).first()
            if current is None:
                return "skipped"
            current_details = copy.deepcopy(current[0]) if isinstance(current[0], dict) else {}
            current_report = current_details.get("skillspector_report")
            if not isinstance(current_report, dict):
                current_report = {}
            if report_fingerprint(current_report) != expected_fingerprint:
                logger.info(
                    "audit %s was re-scanned during review; partial progress discarded",
                    audit_id,
                )
                return "skipped"
            current_details["llm_review"] = partial_meta
            await session.execute(
                update(SecurityAudit)
                .where(SecurityAudit.id == audit_id)
                .values(details=current_details)
            )
            await session.commit()
            return "partial"
    except Exception:
        # 进度落库失败不终止本轮，下一批会带着累积进度再次尝试
        logger.exception("Failed to persist partial review for audit %s", audit_id)
        return "failed"


async def record_failure(
    audit_id: Any,
    error: str,
    *,
    model: str,
    context_mode: str,
    context_stats: dict[str, Any] | None = None,
    reviewed_count: int | None = None,
    timezone: str | None = None,
) -> None:
    """记录失败次数；超过 max_attempts 后不再自动重试。"""
    try:
        async with AsyncSessionLocal() as session:
            current = (
                await session.execute(
                    select(SecurityAudit.details)
                    .where(SecurityAudit.id == audit_id)
                    .with_for_update()
                )
            ).first()
            if current is None:
                return
            details = copy.deepcopy(current[0]) if isinstance(current[0], dict) else {}
            previous = details.get("llm_review")
            previous_meta: dict[str, Any] = previous if isinstance(previous, dict) else {}
            attempts = _coerce_int(previous_meta.get("attempts")) or 0
            failed_meta: dict[str, Any] = {}
            # 保留已复核的游标与判定结论，重试时从断点继续
            for key in (
                "started_at",
                "reviewed_count",
                "false_positive_indices",
                "false_positive_reasons",
                "false_positive_confidences",
                "false_positive_count",
                "low_confidence_indices",
                "low_confidence_reasons",
                "low_confidence_count",
                "findings_total",
                "min_confidence",
                "context_version",
                "context_mode",
                "prompt_version",
            ):
                value = previous_meta.get(key)
                if value is not None:
                    failed_meta[key] = value
            if reviewed_count is not None:
                # 以本次尝试的实际进度为准，避免沿用更早一次的游标
                failed_meta["reviewed_count"] = reviewed_count
            _merge_context_meta(failed_meta, context_stats)
            failed_meta["context_version"] = CONTEXT_VERSION
            failed_meta["context_mode"] = context_mode
            failed_meta["prompt_version"] = PROMPT_VERSION
            failed_meta.update(
                {
                    "status": "failed",
                    "attempts": attempts + 1,
                    # 与复核窗口同一时区，时间串带 +08:00，便于跨时区对账
                    "updated_at": now_in(resolve_timezone(timezone)).isoformat(
                        timespec="seconds"
                    ),
                    "model": model,
                    "error": error[:500],
                }
            )
            details["llm_review"] = failed_meta
            await session.execute(
                update(SecurityAudit).where(SecurityAudit.id == audit_id).values(details=details)
            )
            await session.commit()
            logger.info(
                "audit=%s recorded failure attempts=%d error_type=%s",
                audit_id,
                failed_meta["attempts"],
                error.split(":", 1)[0],
            )
    except Exception:
        logger.exception("Failed to record review failure for audit %s", audit_id)


async def mark_skipped(audit_id: Any, reason: str, *, timezone: str | None = None) -> None:
    try:
        async with AsyncSessionLocal() as session:
            current = (
                await session.execute(
                    select(SecurityAudit.details)
                    .where(SecurityAudit.id == audit_id)
                    .with_for_update()
                )
            ).first()
            if current is None:
                return
            details = copy.deepcopy(current[0]) if isinstance(current[0], dict) else {}
            details["llm_review"] = {
                "status": "skipped",
                "reason": reason,
                "updated_at": now_in(resolve_timezone(timezone)).isoformat(
                    timespec="seconds"
                ),
            }
            await session.execute(
                update(SecurityAudit).where(SecurityAudit.id == audit_id).values(details=details)
            )
            await session.commit()
            logger.debug("audit=%s marked skipped reason=%s", audit_id, reason)
    except Exception:
        logger.exception("Failed to mark audit %s as skipped", audit_id)
