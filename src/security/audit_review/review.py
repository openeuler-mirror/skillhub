"""Single-audit review flow: context, structural verdicts, batch orchestration."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import httpx

from src.core.config import AuditLLMReviewConfig
from src.security.audit_review.analysis import (
    _coerce_confidence,
    _coerce_int,
    _false_positive_threshold,
    _issue_rule_id,
    _parse_manifest_frontmatter,
    _structural_verdict,
    _truncate,
)
from src.security.audit_review.apply import (
    _merge_context_meta,
    _removal_kind,
    apply_review,
)
from src.security.audit_review.constants import (
    _MAX_FALSE_POSITIVE_DETAILS,
    _MAX_REASON_CHARS,
    _STRUCTURAL_RULES,
    PROMPT_VERSION,
)
from src.security.audit_review.context import (
    CONTEXT_VERSION,
    RemoteFetchBudget,
    build_repo_target,
    group_consecutive_by_file,
    new_context_stats,
    record_context,
    render_context,
    resolve_file,
    select_file_batch,
    split_chunks,
)
from src.security.audit_review.log import get_logger
from src.security.audit_review.metrics import current_audit
from src.security.audit_review.prompt import (
    SYSTEM_PROMPT,
    LLMReviewError,
    _match_verdicts,
    build_user_prompt,
)
from src.security.audit_review.queue import _RateLimiter

logger = get_logger("review")


class ReviewDeadlineError(Exception):
    """复核窗口已结束：中止当前审计，但不记失败、不消耗重试次数。"""


def build_partial_meta(
    config: AuditLLMReviewConfig,
    *,
    issues_count: int,
    reviewed_count: int,
    false_positive_indices: set[int],
    false_positive_reasons: list[dict[str, Any]],
    false_positive_confidences: dict[int, float | None] | None = None,
    low_confidence_indices: set[int],
    low_confidence_reasons: list[dict[str, Any]],
    started_at: str | None,
    context_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    meta: dict[str, Any] = {
        "status": "partial",
        "model": config.model,
        "started_at": started_at or now,
        "updated_at": now,
        "findings_total": issues_count,
        "reviewed_count": reviewed_count,
        "false_positive_indices": sorted(false_positive_indices),
        "false_positive_reasons": false_positive_reasons,
        "false_positive_confidences": {
            str(index): confidence
            for index, confidence in (false_positive_confidences or {}).items()
        },
        "false_positive_count": len(false_positive_indices),
        "low_confidence_indices": sorted(low_confidence_indices),
        "low_confidence_reasons": low_confidence_reasons,
        "low_confidence_count": len(low_confidence_indices),
        "min_confidence": config.min_confidence,
        "min_false_positive_confidence": config.min_false_positive_confidence,
        "min_false_positive_confidence_high": (config.min_false_positive_confidence_high),
        "attempts": 0,
    }
    _merge_context_meta(meta, context_stats)
    meta["context_version"] = CONTEXT_VERSION
    meta["context_mode"] = config.context_mode
    meta["prompt_version"] = PROMPT_VERSION
    return meta


async def review_one(
    scheduler: Any,
    client: httpx.AsyncClient,
    limiter: _RateLimiter,
    row: dict[str, Any],
    deadline: datetime | None = None,
) -> str:
    audit_id = row.get("id")
    record = current_audit.get()
    raw_details = row.get("details")
    details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else {}
    raw_report = details.get("skillspector_report")
    report: dict[str, Any] = raw_report if isinstance(raw_report, dict) else {}
    raw_issues = report.get("issues")
    issues: list[Any] = raw_issues if isinstance(raw_issues, list) else []
    if record is not None:
        record.issues_count = len(issues)
    if not issues:
        logger.info("audit=%s skipped reason=no_issues", audit_id)
        if record is not None:
            record.skip_reason = "no_issues"
        await scheduler._mark_skipped(audit_id, "no_issues")
        return "skipped"

    previous = details.get("llm_review")
    previous_meta: dict[str, Any] = previous if isinstance(previous, dict) else {}
    if previous_meta.get("status") in {"completed", "skipped"}:
        reason = f"already_{previous_meta.get('status')}"
        logger.info("audit=%s skipped reason=%s", audit_id, reason)
        if record is not None:
            record.skip_reason = reason
        return "skipped"

    reviewed_count = _coerce_int(previous_meta.get("reviewed_count")) or 0
    reviewed_count = max(0, min(reviewed_count, len(issues)))
    false_positive_indices: set[int] = set()
    false_positive_reasons: list[dict[str, Any]] = []
    low_confidence_indices: set[int] = set()
    low_confidence_reasons: list[dict[str, Any]] = []
    raw_indices = previous_meta.get("false_positive_indices")
    if isinstance(raw_indices, list):
        for value in raw_indices:
            index = _coerce_int(value)
            if index is not None and 0 <= index < len(issues):
                false_positive_indices.add(index)
    raw_reasons = previous_meta.get("false_positive_reasons")
    if isinstance(raw_reasons, list):
        for item in raw_reasons:
            if not isinstance(item, dict):
                continue
            index = _coerce_int(item.get("index"))
            if index is not None and 0 <= index < len(issues):
                false_positive_reasons.append(
                    {
                        "index": index,
                        "reason": _truncate(item.get("reason"), _MAX_REASON_CHARS),
                        "confidence": _coerce_confidence(item.get("confidence")),
                    }
                )
    raw_low_confidence = previous_meta.get("low_confidence_indices")
    if isinstance(raw_low_confidence, list):
        for value in raw_low_confidence:
            index = _coerce_int(value)
            if index is not None and 0 <= index < len(issues):
                low_confidence_indices.add(index)
    raw_low_reasons = previous_meta.get("low_confidence_reasons")
    if isinstance(raw_low_reasons, list):
        for item in raw_low_reasons:
            if not isinstance(item, dict):
                continue
            index = _coerce_int(item.get("index"))
            if index is not None and 0 <= index < len(issues):
                low_confidence_reasons.append(
                    {
                        "index": index,
                        "reason": _truncate(item.get("reason"), _MAX_REASON_CHARS),
                        "confidence": _coerce_confidence(item.get("confidence")),
                    }
                )
    started_at = str(previous_meta.get("started_at") or "") or None
    if previous_meta and (
        previous_meta.get("context_version") != CONTEXT_VERSION
        or previous_meta.get("prompt_version") != PROMPT_VERSION
    ):
        # 上下文/提示词结构升级后旧游标与新的判定结构不兼容，
        # 这里只重置进度，不删除已写入的结论。
        reviewed_count = 0
        false_positive_indices.clear()
        false_positive_reasons.clear()
        low_confidence_indices.clear()
        low_confidence_reasons.clear()
        started_at = None
        logger.info(
            "audit=%s context/prompt version changed (%s/%s -> %s/%s); restarting partial review",
            audit_id,
            previous_meta.get("context_version"),
            previous_meta.get("prompt_version"),
            CONTEXT_VERSION,
            PROMPT_VERSION,
        )
    reasons_by_index = {item["index"]: item["reason"] for item in false_positive_reasons}
    confidence_by_index = {item["index"]: item.get("confidence") for item in false_positive_reasons}
    # 超过展示上限的误报不在 false_positive_reasons 里，confidence
    # 由单独的映射恢复，避免续跑后把已确认的误报重新保留。
    raw_confidences = previous_meta.get("false_positive_confidences")
    if isinstance(raw_confidences, dict):
        for key, value in raw_confidences.items():
            index = _coerce_int(key)
            if index is not None and 0 <= index < len(issues):
                confidence_by_index[index] = _coerce_confidence(value)
    system_prompt = SYSTEM_PROMPT

    batch, cursor, remaining = select_file_batch(
        issues, reviewed_count, scheduler._config.max_findings_per_audit
    )
    skill_name = row.get("skill_name") or row.get("version_name") or ""
    skill_description = row.get("skill_description") or row.get("version_description") or ""
    skill_content = row.get("skill_content") or row.get("version_content") or ""
    raw_components = report.get("components")
    components: list[Any] = raw_components if isinstance(raw_components, list) else []
    file_paths = [
        str(component.get("path"))
        for component in components
        if isinstance(component, dict) and component.get("path")
    ]
    target = build_repo_target(
        repo_url=row.get("skill_repo_url") or row.get("version_repo_url"),
        source_url=row.get("skill_source_url") or row.get("version_source_url"),
        source=row.get("skill_source") or row.get("version_source"),
        skill_id=row.get("skill_skill_id") or row.get("version_skill_id"),
        commit=(
            row.get("audit_commit_id") or row.get("skill_commit_id") or row.get("version_commit_id")
        ),
        cache_root=scheduler._config.repo_cache_root,
    )
    remote_budget = RemoteFetchBudget(scheduler._config.remote_fetch_max_files_per_audit)
    context_stats = new_context_stats(scheduler._context_config)

    logger.debug(
        "reviewing audit=%s issues=%d batch=%d cursor=%d->%d remaining=%d",
        audit_id,
        len(issues),
        len(batch),
        reviewed_count,
        cursor,
        remaining,
    )
    chunk_size = max(1, scheduler._config.max_findings_per_call)
    structural_candidates = {
        index for index in batch if _issue_rule_id(issues[index]) in _STRUCTURAL_RULES
    }
    kept_incomplete_context = 0
    structural_reviewed = 0
    structural_false_positive = 0
    structural_confirmed = 0
    confirmed_verdicts: dict[int, dict[str, Any]] = {}
    kept_false_verdicts: dict[int, dict[str, Any]] = {}
    manifest_loaded = False
    manifest_cache: dict[str, Any] | None = None

    async def load_manifest() -> dict[str, Any] | None:
        nonlocal manifest_loaded, manifest_cache
        if manifest_loaded:
            return manifest_cache
        manifest_loaded = True
        if target is None:
            return None
        for file_name in ("SKILL.md", "skill.md"):
            resolved = await resolve_file(
                client,
                target,
                file_name,
                scheduler._context_config,
                scheduler._remote_tokens,
                remote_budget,
                scheduler._file_cache,
            )
            if resolved.content is not None:
                manifest_cache = _parse_manifest_frontmatter(resolved.content)
                return manifest_cache
        return None

    def record_verdict(
        index: int,
        verdict: dict[str, Any],
        *,
        complete_context: bool,
        structural: bool,
    ) -> None:
        nonlocal kept_incomplete_context, structural_reviewed
        nonlocal structural_false_positive, structural_confirmed
        threshold = _false_positive_threshold(
            issues[index],
            scheduler._config.min_false_positive_confidence,
            scheduler._config.min_false_positive_confidence_high,
        )
        removal = _removal_kind(verdict, scheduler._config.min_confidence, threshold)
        if removal is None and verdict.get("is_vulnerability") is False:
            # 误报置信度不足：保留，并交给 apply_review 计入门槛统计
            kept_false_verdicts[index] = {
                "is_vulnerability": False,
                "confidence": verdict.get("confidence"),
                "reason": verdict.get("reason") or "",
            }
            if structural:
                structural_reviewed += 1
            return
        if removal is not None and not complete_context:
            # 上下文不完整时不做任何删除：误报和低置信度命中都保留
            kept_incomplete_context += 1
            if structural:
                structural_reviewed += 1
            return
        if removal == "false_positive":
            false_positive_indices.add(index)
            reason = verdict.get("reason") or ""
            # confidence 必须为全部误报保留：理由列表按展示上限截断，
            # 但截断后的误报仍要按门槛决定是否删除。
            reasons_by_index[index] = reason
            confidence_by_index[index] = verdict.get("confidence")
            if record is not None:
                rule = _issue_rule_id(issues[index]) or "unknown"
                record.false_positive_rules[rule] += 1
            if len(false_positive_reasons) < _MAX_FALSE_POSITIVE_DETAILS:
                false_positive_reasons.append(
                    {
                        "index": index,
                        "reason": reason,
                        "confidence": verdict.get("confidence"),
                    }
                )
            if structural:
                structural_false_positive += 1
        elif removal == "low_confidence":
            low_confidence_indices.add(index)
            if len(low_confidence_reasons) < _MAX_FALSE_POSITIVE_DETAILS:
                low_confidence_reasons.append(
                    {
                        "index": index,
                        "reason": verdict.get("reason") or "",
                        "confidence": verdict.get("confidence"),
                    }
                )
        elif verdict.get("is_vulnerability") is True:
            # 保留确认为真的命中，让 apply_review 正确统计
            # findings_reviewed / findings_confirmed
            confirmed_verdicts[index] = {
                "is_vulnerability": True,
                "confidence": verdict.get("confidence"),
                "reason": verdict.get("reason") or "",
            }
            if structural:
                structural_confirmed += 1
        if structural:
            structural_reviewed += 1

    async def process_chunk(chunk: list[int], file_name: str) -> None:
        nonlocal reviewed_count
        if deadline is not None and datetime.now() >= deadline:
            raise ReviewDeadlineError
        resolved = await resolve_file(
            client,
            target,
            file_name,
            scheduler._context_config,
            scheduler._remote_tokens,
            remote_budget,
            scheduler._file_cache,
        )
        if record is not None:
            degraded_reason = (
                resolved.reason
                if resolved.source == "snippet" and scheduler._config.context_mode != "snippet"
                else ""
            )
            if record.note_context(resolved.source, degraded_reason):
                logger.info(
                    "audit=%s file=%s context degraded to snippet reason=%s",
                    audit_id,
                    file_name,
                    degraded_reason,
                )
        llm_chunk = list(chunk)
        if any(index in structural_candidates for index in chunk):
            manifest = await load_manifest()
            if manifest is not None:
                llm_chunk = []
                for index in chunk:
                    verdict = (
                        _structural_verdict(issues[index], manifest)
                        if index in structural_candidates
                        else None
                    )
                    if verdict is None:
                        llm_chunk.append(index)
                    else:
                        record_verdict(
                            index,
                            verdict,
                            complete_context=True,
                            structural=True,
                        )
        if llm_chunk:
            context = render_context(
                resolved,
                [issues[index] for index in llm_chunk],
                scheduler._context_config,
                file=file_name,
            )
            record_context(context_stats, context)
            if (
                record is not None
                and context.mode == "snippet"
                and scheduler._config.context_mode != "snippet"
            ):
                reason = context.reason or "context_unavailable"
                if record.note_context("snippet", reason):
                    logger.info(
                        "audit=%s file=%s context degraded to snippet reason=%s",
                        audit_id,
                        file_name,
                        reason,
                    )
            prompt = build_user_prompt(
                skill_name=str(skill_name),
                skill_description=str(skill_description),
                skill_content=str(skill_content),
                file_paths=file_paths,
                issues=issues,
                chunk=llm_chunk,
                total_issues=len(issues),
                skipped=max(0, len(issues) - len(llm_chunk)),
                file_contexts={file_name: context.text} if context.text else None,
            )
            raw_verdicts = await scheduler._call_llm_json(
                client,
                limiter,
                prompt,
                len(llm_chunk),
                system_prompt=system_prompt,
            )
            matched = _match_verdicts(raw_verdicts, llm_chunk, issues)
            if not matched:
                raise LLMReviewError("model verdicts did not match any finding")
            complete_context = (
                context.mode in {"full", "window"}
                and resolved.source in {"cache", "remote"}
                and bool(resolved.content)
            )
            for index, verdict in matched.items():
                record_verdict(
                    index,
                    verdict,
                    complete_context=complete_context,
                    structural=False,
                )
        reviewed_count += len(chunk)
        if reviewed_count < len(issues):
            await scheduler._persist_partial(
                audit_id,
                report,
                scheduler._build_partial_meta(
                    issues_count=len(issues),
                    reviewed_count=reviewed_count,
                    false_positive_indices=false_positive_indices,
                    false_positive_reasons=false_positive_reasons,
                    false_positive_confidences=confidence_by_index,
                    low_confidence_indices=low_confidence_indices,
                    low_confidence_reasons=low_confidence_reasons,
                    started_at=started_at,
                    context_stats=context_stats,
                ),
            )

    try:
        for file_name, group in group_consecutive_by_file(issues, batch):
            for chunk in split_chunks(group, chunk_size):
                await process_chunk(chunk, file_name)
    except ReviewDeadlineError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("audit %s review failed: %s", audit_id, error, exc_info=True)
        if record is not None:
            record.error = error
            record.error_type = type(exc).__name__
        await scheduler._record_failure(
            audit_id,
            error,
            context_stats=context_stats,
            reviewed_count=reviewed_count,
        )
        return "failed"

    reviewed_at = datetime.now().isoformat(timespec="seconds")
    if reviewed_count < len(issues):
        # 本轮达到单条审计上限：保存游标，下一轮窗口从 reviewed_count 继续
        await scheduler._persist_partial(
            audit_id,
            report,
            scheduler._build_partial_meta(
                issues_count=len(issues),
                reviewed_count=reviewed_count,
                false_positive_indices=false_positive_indices,
                false_positive_reasons=false_positive_reasons,
                false_positive_confidences=confidence_by_index,
                low_confidence_indices=low_confidence_indices,
                low_confidence_reasons=low_confidence_reasons,
                started_at=started_at,
                context_stats=context_stats,
            ),
        )
        logger.info(
            "audit=%s partial reviewed=%d/%d remaining=%d",
            audit_id,
            reviewed_count,
            len(issues),
            remaining,
        )
        if record is not None:
            record.reviewed = reviewed_count
        return "partial"

    verdicts: dict[int, dict[str, Any]] = {
        index: {
            "is_vulnerability": False,
            "confidence": confidence_by_index.get(index),
            "reason": reasons_by_index.get(index, ""),
        }
        for index in false_positive_indices
    }
    verdicts.update(confirmed_verdicts)
    verdicts.update(kept_false_verdicts)
    for index in low_confidence_indices:
        verdict = next(
            (item for item in low_confidence_reasons if item.get("index") == index),
            {},
        )
        confidence = verdict.get("confidence")
        verdicts[index] = {
            "is_vulnerability": True,
            # 恢复的低置信度项必须继续低于门槛，缺值按 0.0 处理，
            # 否则会在断点续跑后重新变成保留命中
            "confidence": confidence if confidence is not None else 0.0,
            "reason": verdict.get("reason") or "",
        }
    application = apply_review(
        details=details,
        risk_signals=row.get("risk_signals"),
        current_risk_level=str(row.get("risk_level") or "unknown"),
        verdicts=verdicts,
        model=scheduler._config.model,
        reviewed_at=reviewed_at,
        skipped=0,
        reviewed_total=len(issues),
        started_at=started_at,
        min_confidence=scheduler._config.min_confidence,
        min_false_positive_confidence=scheduler._config.min_false_positive_confidence,
        min_false_positive_confidence_high=(scheduler._config.min_false_positive_confidence_high),
        kept_incomplete_context=kept_incomplete_context,
        structural_reviewed=structural_reviewed,
        structural_false_positive=structural_false_positive,
        structural_confirmed=structural_confirmed,
        context_meta=context_stats,
    )
    outcome = await scheduler._persist(row, report, application)
    if record is not None:
        meta = application.details.get("llm_review") or {}
        record.reviewed = application.reviewed
        record.confirmed = application.confirmed
        record.false_positive = application.false_positives
        record.low_confidence = application.low_confidence
        record.score_before = meta.get("score_before")
        record.score_after = application.score
        record.level_before = str(meta.get("risk_level_before") or "")
        record.level_after = application.risk_level
    if application.details.get("llm_review", {}).get("all_removed"):
        logger.warning(
            "audit=%s all findings removed after review; applied score=%s level=%s",
            audit_id,
            application.score,
            application.risk_level,
        )
    # 每条审计的完整结果日志统一由调度器的 _run 输出（含 skill 标识、耗时、
    # 上下文来源与 LLM 调用统计），此处不再重复打印。
    return outcome
