"""Apply review verdicts: remove false positives and recompute the score."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from src.security.audit_review.analysis import (
    _as_score,
    _coerce_int,
    _false_positive_threshold,
    _issue_location,
    _issue_rule_id,
    compute_skillspector_score,
    level_from_score,
    recommendation_for_level,
)
from src.security.audit_review.constants import (
    _MAX_FALSE_POSITIVE_DETAILS,
    PROMPT_VERSION,
)

# ═══════════════════════════════════════════════════════════════════════════
# 复核结果应用（纯函数，便于单测）
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class ReviewApplication:
    details: dict[str, Any]
    risk_signals: list[dict[str, Any]]
    risk_level: str
    score: int | None
    reviewed: int
    confirmed: int
    false_positives: int
    skipped: int
    # 置信度低于阈值的确认项：按上游口径视为丢弃，单独计数便于排障
    low_confidence: int = 0


def _issue_key(issue: Any) -> tuple[str, str, int | None]:
    location = _issue_location(issue)
    return (
        _issue_rule_id(issue),
        str(location.get("file") or ""),
        _coerce_int(location.get("start_line")),
    )


def _signal_key(signal: Any) -> tuple[str, str, int | None]:
    if not isinstance(signal, dict):
        return ("", "", None)
    raw_data = signal.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    raw_location = data.get("location")
    location: dict[str, Any] = raw_location if isinstance(raw_location, dict) else {}
    return (
        str(signal.get("id") or "").strip().upper(),
        str(location.get("file") or ""),
        _coerce_int(location.get("start_line")),
    )


def _removal_kind(
    verdict: dict[str, Any],
    min_confidence: float,
    min_false_positive_confidence: float = 0.0,
) -> str | None:
    """判定一条 verdict 是否应当删除命中。

    - 显式 ``is_vulnerability=false`` 且 confidence 达到误报删除门槛：删除；
    - 误报置信度不足（缺值或低于门槛）：保留，交给人工/后续复核；
    - 确认为真但 ``confidence`` 低于 ``min_confidence``：与上游
      ``meta_analyzer`` 一致，视为不可信结论丢弃；``min_confidence<=0``
      时关闭该门槛。
    """
    if verdict.get("is_vulnerability") is False:
        if min_false_positive_confidence > 0:
            confidence = verdict.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                return None
            if confidence < min_false_positive_confidence:
                return None
        return "false_positive"
    if min_confidence <= 0:
        return None
    confidence = verdict.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        if confidence < min_confidence:
            return "low_confidence"
    return None


def _merge_context_meta(
    meta: dict[str, Any],
    context_meta: dict[str, Any] | None,
) -> None:
    """Attach context statistics without overriding review status fields."""
    if not context_meta:
        return
    for key, value in context_meta.items():
        meta.setdefault(key, value)


def apply_review(
    *,
    details: dict[str, Any],
    risk_signals: list[dict[str, Any]] | None,
    current_risk_level: str,
    verdicts: dict[int, dict[str, Any]],
    model: str,
    reviewed_at: str,
    skipped: int,
    reviewed_total: int | None = None,
    started_at: str | None = None,
    min_confidence: float = 0.0,
    min_false_positive_confidence: float = 0.0,
    min_false_positive_confidence_high: float = 0.0,
    kept_incomplete_context: int = 0,
    structural_reviewed: int = 0,
    structural_false_positive: int = 0,
    structural_confirmed: int = 0,
    context_meta: dict[str, Any] | None = None,
) -> ReviewApplication:
    """把逐条 verdict 应用到审计详情，返回待写库的新值。

    ``verdicts`` 的 key 是 ``report.issues`` 的原始下标。删除规则：显式
    ``is_vulnerability=false`` 且置信度达到误报删除门槛的误报，以及确认
    为真但置信度低于 ``min_confidence`` 的命中（与上游 ``meta_analyzer``
    一致）；未出现的下标一律保留。确认项保持原始 issue 结构，不写入额外
    字段。``min_false_positive_confidence`` 为 0 时关闭误报置信度门槛。

    分数用 :func:`compute_skillspector_score` 对**保留命中**重算，与
    SkillSpector 原生算法完全一致；没有命中被删除时结果与原分相同。
    ``reviewed_total`` 用于分批续跑完成时标注实际复核过的总条数。
    """
    updated = copy.deepcopy(details or {})
    report = updated.get("skillspector_report")
    if not isinstance(report, dict):
        report = {}
    raw_issues = report.get("issues")
    if not isinstance(raw_issues, list):
        raw_issues = []

    kept_issues: list[Any] = []
    false_positive_details: list[dict[str, Any]] = []
    low_confidence_details: list[dict[str, Any]] = []
    reviewed = confirmed = false_positives = low_confidence = 0
    false_positive_below_threshold = 0
    for index, issue in enumerate(raw_issues):
        verdict = verdicts.get(index)
        if verdict is None:
            kept_issues.append(issue)
            continue
        threshold = _false_positive_threshold(
            issue, min_false_positive_confidence, min_false_positive_confidence_high
        )
        removal = _removal_kind(verdict, min_confidence, threshold)
        if removal is not None:
            reviewed += 1
            location = _issue_location(issue)
            detail = {
                "id": issue.get("id") if isinstance(issue, dict) else None,
                "file": location.get("file"),
                "start_line": location.get("start_line"),
                "reason": verdict.get("reason") or "",
                "confidence": verdict.get("confidence"),
            }
            if removal == "false_positive":
                false_positives += 1
                if len(false_positive_details) < _MAX_FALSE_POSITIVE_DETAILS:
                    false_positive_details.append(detail)
            else:
                low_confidence += 1
                if len(low_confidence_details) < _MAX_FALSE_POSITIVE_DETAILS:
                    low_confidence_details.append(detail)
            continue
        if verdict.get("is_vulnerability") is False:
            # 模型判了误报，但置信度不足删除门槛：保留命中
            false_positive_below_threshold += 1
            kept_issues.append(issue)
            continue
        if verdict.get("is_vulnerability") is not True:
            kept_issues.append(issue)
            continue
        reviewed += 1
        confirmed += 1
        kept_issues.append(issue)

    risk_assessment = report.get("risk_assessment")
    if not isinstance(risk_assessment, dict):
        risk_assessment = {}
    original_score = _as_score(updated.get("skillspector_score"))
    if original_score is None:
        original_score = _as_score(risk_assessment.get("score"))

    raw_metadata = report.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    new_score = compute_skillspector_score(
        kept_issues, bool(metadata.get("has_executable_scripts"))
    )
    new_level = level_from_score(new_score)
    recommendation = recommendation_for_level(new_level)

    removed_total = false_positives + low_confidence
    all_removed = bool(raw_issues) and removed_total >= len(raw_issues)

    if reviewed_total is not None:
        reviewed = max(0, min(reviewed_total, len(raw_issues)))
        confirmed = max(0, len(raw_issues) - removed_total)

    # 只过滤能唯一定位到被删命中的静态信号；无法定位的信号保留，避免误删
    signals = list(risk_signals) if isinstance(risk_signals, list) else []
    filtered_keys = {
        key
        for index, verdict in verdicts.items()
        if 0 <= index < len(raw_issues)
        and _removal_kind(
            verdict,
            min_confidence,
            _false_positive_threshold(
                raw_issues[index],
                min_false_positive_confidence,
                min_false_positive_confidence_high,
            ),
        )
        is not None
        for key in (_issue_key(raw_issues[index]),)
        if key[1] or key[2] is not None
    }
    new_signals = [
        signal
        for signal in signals
        if not (isinstance(signal, dict) and _signal_key(signal) in filtered_keys)
    ]

    report["issues"] = kept_issues
    report["risk_assessment"] = {
        **risk_assessment,
        "score": new_score,
        "severity": new_level.upper(),
        "recommendation": recommendation,
    }
    updated["skillspector_report"] = report
    updated["skillspector_score"] = new_score
    updated["recommendation"] = recommendation
    if "risk_level" in updated:
        updated["risk_level"] = new_level
    if "risk_signals" in updated:
        updated["risk_signals"] = new_signals
    review_meta: dict[str, Any] = {
        "status": "completed",
        "all_removed": all_removed,
        "applied": True,
        "model": model,
        "started_at": started_at or reviewed_at,
        "reviewed_at": reviewed_at,
        "findings_total": len(raw_issues),
        "reviewed_count": len(raw_issues),
        "findings_reviewed": reviewed,
        "findings_confirmed": confirmed,
        "findings_false_positive": false_positives,
        "findings_low_confidence": low_confidence,
        "findings_false_positive_below_threshold": false_positive_below_threshold,
        "findings_removed": removed_total,
        "findings_kept_incomplete_context": kept_incomplete_context,
        "findings_structural_reviewed": structural_reviewed,
        "findings_structural_false_positive": structural_false_positive,
        "findings_structural_confirmed": structural_confirmed,
        "false_positive_count": false_positives,
        "low_confidence_count": low_confidence,
        "findings_skipped": skipped,
        "attempts": 0,
        "score_before": original_score,
        "score_after": new_score,
        "risk_level_before": current_risk_level,
        "risk_level_after": new_level,
        "false_positive_findings": false_positive_details,
        "false_positive_findings_truncated": false_positives > len(false_positive_details),
        "low_confidence_findings": low_confidence_details,
        "low_confidence_findings_truncated": low_confidence > len(low_confidence_details),
        "min_confidence": min_confidence,
        "min_false_positive_confidence": min_false_positive_confidence,
        "min_false_positive_confidence_high": min_false_positive_confidence_high,
        "prompt_version": PROMPT_VERSION,
    }
    _merge_context_meta(review_meta, context_meta)
    updated["llm_review"] = review_meta

    return ReviewApplication(
        details=updated,
        risk_signals=new_signals,
        risk_level=new_level,
        score=new_score,
        reviewed=reviewed,
        confirmed=confirmed,
        false_positives=false_positives,
        skipped=skipped,
        low_confidence=low_confidence,
    )
