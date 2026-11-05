"""LLM 复核轮次的指标收集、日志汇总与结果文件落盘。

与 discover 定时任务保持同一套可观测口径：

- 每条审计一份 :class:`AuditRecord`（skill 标识、耗时、上下文来源、LLM 调用
  次数、分数变化、失败/跳过原因）；
- 每轮一份 :class:`RunStats`，负责轮次汇总日志（平均/最慢、token、限流等待、
  误报规则 Top N、失败/跳过明细）与结构化结果 JSON；
- 结果文件写入 ``audit-review-<时间戳>.json``，字段内嵌 ``<key>_note`` 说明，
  并按 ``result_keep_days`` / ``result_max_files`` 清理历史文件。

结果文件复用 ``src.skills.discover_result.save_discover_result_file``，但关闭
其成功日志（``log=False``），改由本模块打 ``[AuditLLMReview]`` 前缀日志。
"""

from __future__ import annotations

import time
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.security.audit_review.log import get_logger

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型提示
    from src.core.config import AuditLLMReviewConfig

logger = get_logger("metrics")

RESULT_PREFIX = "audit-review"

# 失败/跳过明细在日志与结果文件中最多列出的条数，超出只计数
DETAIL_LIMIT = 50
# 日志中逐条打印的失败明细上限，避免恶意/异常场景刷屏
LOG_DETAIL_LIMIT = 20

COUNTER_NOTES: dict[str, str] = {
    "reviewed": "复核完成并回写数据库的审计数",
    "partial": "达到单条审计上限、保存游标等待下一轮继续的审计数",
    "failed": "本轮复核失败（模型/网络/解析异常）的审计数",
    "skipped": "本轮跳过（无命中、已完成或被重扫丢弃）的审计数",
    "total": "本轮处理结果数（同一审计跨 pass 推进会计多次，可能大于唯一审计数）",
}

LLM_NOTES: dict[str, str] = {
    "calls": "实际发出的 LLM 请求次数（含重试）",
    "retries": "因超时/限流/可重试状态码触发的重试次数",
    "estimated_tokens": "按 prompt 字符数 / 4 + 输出预算估算的 token 总量",
    "seconds": "LLM 请求累计耗时（秒，含重试等待）",
    "slow_calls": "单次耗时超过 slow_llm_seconds 的请求数",
    "rate_limit_wait_seconds": "客户端限流累计等待时长（秒）",
    "rate_limit_events": "触发客户端限流等待的次数",
}

CONTEXT_NOTES: dict[str, str] = {
    "sources": "复核上下文来源分布 {cache|remote|snippet: count}",
    "reasons": "上下文降级到 snippet 的原因分布",
}

# current_audit 指向当前正在复核的审计记录；并发任务各自在独立上下文中
# 设置，LLM 调用层与上下文解析层据此累加本条的统计。
current_audit: ContextVar[AuditRecord | None] = ContextVar(
    "audit_review_current_audit", default=None
)


@dataclass
class AuditRecord:
    """单条审计的复核过程与结果指标。"""

    audit_id: str
    skill_id: str = ""
    name: str = ""
    risk_level: str = ""
    issues_count: int = 0
    outcome: str = ""
    reviewed: int = 0
    confirmed: int = 0
    false_positive: int = 0
    low_confidence: int = 0
    score_before: Any = None
    score_after: Any = None
    level_before: str = ""
    level_after: str = ""
    duration_seconds: float = 0.0
    context_source: str = ""
    context_degraded: bool = False
    context_degrade_reason: str = ""
    llm_calls: int = 0
    llm_retries: int = 0
    slow_llm_calls: int = 0
    estimated_tokens: int = 0
    llm_seconds: float = 0.0
    error: str = ""
    error_type: str = ""
    skip_reason: str = ""
    false_positive_rules: Counter[str] = field(default_factory=Counter)

    def note_context(self, source: str, reason: str = "") -> bool:
        """记录上下文来源；cache/remote 优先于 snippet 作为最终来源。

        返回本次是否首次标记为降级（snippet + 具体原因），调用方据此决定
        是否打印降级日志，避免同一审计多文件重复刷屏。
        """
        if source in {"cache", "remote"}:
            self.context_source = source
        elif not self.context_source:
            self.context_source = source or "snippet"
        newly_degraded = False
        if source == "snippet" and reason and not self.context_degraded:
            self.context_degraded = True
            self.context_degrade_reason = reason
            newly_degraded = True
        return newly_degraded

    def to_dict(self) -> dict[str, Any]:
        false_positive_rules = dict(self.false_positive_rules)
        return {
            "audit_id": self.audit_id,
            "skill_id": self.skill_id,
            "name": self.name,
            "risk_level": self.risk_level,
            "issues_count": self.issues_count,
            "outcome": self.outcome,
            "reviewed": self.reviewed,
            "confirmed": self.confirmed,
            "false_positive": self.false_positive,
            "low_confidence": self.low_confidence,
            "score_before": self.score_before,
            "score_after": self.score_after,
            "level_before": self.level_before,
            "level_after": self.level_after,
            "duration_seconds": round(self.duration_seconds, 3),
            "context_source": self.context_source,
            "context_degraded": self.context_degraded,
            "context_degrade_reason": self.context_degrade_reason,
            "llm_calls": self.llm_calls,
            "llm_retries": self.llm_retries,
            "slow_llm_calls": self.slow_llm_calls,
            "estimated_tokens": self.estimated_tokens,
            "llm_seconds": round(self.llm_seconds, 3),
            "error": self.error,
            "error_type": self.error_type,
            "skip_reason": self.skip_reason,
            "false_positive_rules": false_positive_rules,
        }


class RunStats:
    """一轮复核的累计指标与汇总输出。"""

    def __init__(self, run_id: str, config: AuditLLMReviewConfig):
        self.run_id = run_id
        self.config = config
        self.started_at = datetime.now()
        self.counters: dict[str, int] = {
            "reviewed": 0,
            "partial": 0,
            "failed": 0,
            "skipped": 0,
        }
        self.audits: list[AuditRecord] = []
        self.skip_reasons: Counter[str] = Counter()
        self.context_sources: Counter[str] = Counter()
        self.context_reasons: Counter[str] = Counter()
        self.false_positive_rules: Counter[str] = Counter()
        self.llm_calls = 0
        self.llm_retries = 0
        self.estimated_tokens = 0
        self.llm_seconds = 0.0
        self.slow_llm_calls = 0
        self.rate_limit_wait_seconds = 0.0
        self.rate_limit_events = 0
        self.stopped = "no_work"

    def record(self, audit: AuditRecord) -> None:
        """登记一条审计结果并合并全局统计。"""
        outcome = audit.outcome or "skipped"
        self.audits.append(audit)
        if outcome in self.counters:
            self.counters[outcome] += 1
        if outcome == "skipped" and audit.skip_reason:
            self.skip_reasons[audit.skip_reason] += 1
        if audit.context_source:
            self.context_sources[audit.context_source] += 1
        if audit.context_degrade_reason:
            self.context_reasons[audit.context_degrade_reason] += 1
        self.false_positive_rules.update(audit.false_positive_rules)
        self.llm_calls += audit.llm_calls
        self.llm_retries += audit.llm_retries
        self.slow_llm_calls += audit.slow_llm_calls
        self.estimated_tokens += audit.estimated_tokens
        self.llm_seconds += audit.llm_seconds

    @property
    def total(self) -> int:
        return len(self.audits)

    def summary(self) -> dict[str, Any]:
        """兼容调度器原有返回结构（reviewed/partial/failed/skipped/stopped）。"""
        return {
            "reviewed": self.counters["reviewed"],
            "partial": self.counters["partial"],
            "failed": self.counters["failed"],
            "skipped": self.counters["skipped"],
            "stopped": self.stopped,
        }

    def apply_limiter_stats(self, limiter: Any) -> None:
        """把客户端限流器的累计等待统计并入本轮。"""
        self.rate_limit_wait_seconds = round(
            float(getattr(limiter, "total_wait_seconds", 0.0) or 0.0), 3
        )
        self.rate_limit_events = int(getattr(limiter, "wait_events", 0) or 0)

    def log_summary(self, duration_seconds: float, result_path: Path | None = None) -> None:
        """输出与 discover 轮次汇总对齐的多行结构化日志。"""
        total = self.total
        avg = duration_seconds / total if total else 0.0
        slowest = sorted(
            self.audits, key=lambda item: item.duration_seconds, reverse=True
        )[:3]
        slowest_text = (
            ", ".join(
                f"audit={item.audit_id}({item.issues_count})={item.duration_seconds:.1f}s"
                for item in slowest
            )
            or "n/a"
        )
        logger.info(
            "run finished: id=%s reviewed=%d partial=%d failed=%d skipped=%d "
            "total=%d duration=%.0fs avg=%.1fs/audit stopped=%s slowest: %s",
            self.run_id,
            self.counters["reviewed"],
            self.counters["partial"],
            self.counters["failed"],
            self.counters["skipped"],
            total,
            duration_seconds,
            avg,
            self.stopped,
            slowest_text,
        )
        logger.info(
            "llm stats: calls=%d retries=%d tokens~%d llm_seconds=%.1fs "
            "slow_calls=%d rate_limit_wait=%.0fs rate_limit_events=%d",
            self.llm_calls,
            self.llm_retries,
            self.estimated_tokens,
            self.llm_seconds,
            self.slow_llm_calls,
            self.rate_limit_wait_seconds,
            self.rate_limit_events,
        )
        if self.context_sources or self.context_reasons:
            logger.info(
                "context stats: sources=%s degraded=%s",
                dict(self.context_sources),
                dict(self.context_reasons),
            )
        if self.false_positive_rules:
            top = ", ".join(
                f"{rule}={count}"
                for rule, count in self.false_positive_rules.most_common(10)
            )
            logger.info("false positives by rule (top 10): %s", top)
        if self.skip_reasons:
            logger.info("skip reasons: %s", dict(self.skip_reasons))

        failed = [item for item in self.audits if item.outcome == "failed"]
        for item in failed[:LOG_DETAIL_LIMIT]:
            logger.warning(
                "failed audit: audit=%s skill=%s name=%s issues=%d error_type=%s error=%s",
                item.audit_id,
                item.skill_id,
                item.name,
                item.issues_count,
                item.error_type or "unknown",
                item.error[:200],
            )
        if len(failed) > LOG_DETAIL_LIMIT:
            logger.warning(
                "failed audits truncated: %d more not listed (limit=%d)",
                len(failed) - LOG_DETAIL_LIMIT,
                LOG_DETAIL_LIMIT,
            )
        skipped = [item for item in self.audits if item.outcome == "skipped"]
        for item in skipped[:LOG_DETAIL_LIMIT]:
            logger.info(
                "skipped audit: audit=%s skill=%s name=%s reason=%s",
                item.audit_id,
                item.skill_id,
                item.name,
                item.skip_reason or "unknown",
            )
        if len(skipped) > LOG_DETAIL_LIMIT:
            logger.info(
                "skipped audits truncated: %d more not listed (limit=%d)",
                len(skipped) - LOG_DETAIL_LIMIT,
                LOG_DETAIL_LIMIT,
            )
        if result_path is not None:
            logger.info("result saved to %s", result_path)

    def document(self, finished_at: datetime | None = None) -> dict[str, Any]:
        """构建结果文件文档；每个统计块内嵌 ``<key>_note`` 说明。"""
        finished = finished_at or datetime.now()
        duration = max(0.0, (finished - self.started_at).total_seconds())
        slowest = sorted(
            self.audits, key=lambda item: item.duration_seconds, reverse=True
        )[:3]
        failed = [item for item in self.audits if item.outcome == "failed"]
        skipped = [item for item in self.audits if item.outcome == "skipped"]
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_seconds": round(duration, 3),
            "stopped": self.stopped,
            "window": {
                "start_time": self.config.start_time,
                "end_time": self.config.end_time,
                "timezone": _local_timezone_name(),
            },
            "model": {
                "name": self.config.model,
                "base_url": self.config.base_url,
                "context_mode": self.config.context_mode,
                "concurrency": self.config.concurrency,
                "rpm": self.config.rpm,
                "tpm": self.config.tpm,
                "max_findings_per_call": self.config.max_findings_per_call,
                "max_findings_per_audit": self.config.max_findings_per_audit,
                "max_attempts": self.config.max_attempts,
                "min_confidence": self.config.min_confidence,
                "min_false_positive_confidence": self.config.min_false_positive_confidence,
                "min_false_positive_confidence_high": (
                    self.config.min_false_positive_confidence_high
                ),
            },
            "counters": _with_notes(
                {**self.counters, "total": self.total}, COUNTER_NOTES
            ),
            "avg_seconds_per_audit": round(duration / self.total, 3) if self.total else 0.0,
            "slowest_audits": [
                {
                    "audit_id": item.audit_id,
                    "issues_count": item.issues_count,
                    "duration_seconds": round(item.duration_seconds, 3),
                }
                for item in slowest
            ],
            "llm": _with_notes(
                {
                    "calls": self.llm_calls,
                    "retries": self.llm_retries,
                    "estimated_tokens": self.estimated_tokens,
                    "seconds": round(self.llm_seconds, 3),
                    "slow_calls": self.slow_llm_calls,
                    "rate_limit_wait_seconds": self.rate_limit_wait_seconds,
                    "rate_limit_events": self.rate_limit_events,
                },
                LLM_NOTES,
            ),
            "context": _with_notes(
                {
                    "sources": dict(self.context_sources),
                    "reasons": dict(self.context_reasons),
                },
                CONTEXT_NOTES,
            ),
            "false_positive_rules": dict(self.false_positive_rules.most_common()),
            "skip_reasons": dict(self.skip_reasons),
            "failed_audits": [item.to_dict() for item in failed[:DETAIL_LIMIT]],
            "failed_audits_truncated": len(failed) > DETAIL_LIMIT,
            "skipped_audits": [item.to_dict() for item in skipped[:DETAIL_LIMIT]],
            "skipped_audits_truncated": len(skipped) > DETAIL_LIMIT,
            "audits": [item.to_dict() for item in self.audits],
        }


def _with_notes(data: dict[str, Any], notes: dict[str, str]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, value in data.items():
        merged[key] = value
        merged[f"{key}_note"] = notes.get(key, "")
    return merged


def _local_timezone_name() -> str:
    """返回本地时区名（容器内由 TZ 决定），失败时返回 local。"""
    try:
        return datetime.now().astimezone().tzname() or "local"
    except Exception:  # pragma: no cover - 极端环境下 tzinfo 缺失
        return "local"


def resolve_result_dir(configured: str) -> Path:
    """结果目录：优先 audit_llm_review.result_dir，否则 storage.local_path/logs。

    不跟随 discover_scheduler.result_dir，避免两类任务的结果文件混在同一目录。
    """
    text = (configured or "").strip()
    if text:
        return Path(text).expanduser()
    from src.core.config import get_settings

    return Path(get_settings().storage.local_path) / "logs"


def prune_result_files(directory: Path, keep_days: int, max_files: int) -> int:
    """清理历史结果文件：按 mtime 倒序保留最近 max_files 份且不早于 keep_days。

    删除失败只跳过，不影响主流程；返回实际删除数量。
    """
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


def save_run_result(document: dict[str, Any], config: AuditLLMReviewConfig) -> Path | None:
    """保存一轮结果并按配置清理历史文件。"""
    from src.skills.discover_result import save_discover_result_file

    target_dir = resolve_result_dir(config.result_dir)
    path = save_discover_result_file(
        document,
        result_dir=str(target_dir),
        file_prefix=RESULT_PREFIX,
        log=False,
    )
    if path is None:
        logger.warning("failed to save result file (dir=%s)", config.result_dir or "default")
        return None
    removed = prune_result_files(
        path.parent,
        keep_days=config.result_keep_days,
        max_files=config.result_max_files,
    )
    if removed:
        logger.info(
            "pruned %d old result file(s) (keep_days=%d, max_files=%d)",
            removed,
            config.result_keep_days,
            config.result_max_files,
        )
    return path
