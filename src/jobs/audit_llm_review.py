"""后台定时 LLM 复核调度器（Skill 安全审计误报复核）。

在每天 ``audit_llm_review.start_time`` ~ ``end_time`` 窗口内，分批读取
``security_audits`` 中尚未复核过的 SkillSpector 风险项，调用 OpenAI 兼容
接口让模型逐条判定是否为真实风险，剔除误报后重算风险分与等级并回写：

- ``security_audits.risk_signals`` / ``risk_level``
- ``security_audits.details.skillspector_report.issues`` / ``risk_assessment``
- ``security_audits.details.skillspector_score`` / ``recommendation``
- ``skills.risk_score`` 或 ``skill_versions.risk_score``
- ``security_audits.details.llm_review``（复核元数据与误报明细）

设计约束（与 SkillSpector meta_analyzer 的教训保持一致）：

- 只有模型显式判定 ``is_vulnerability=false`` 的命中才会被删除；未出现在
  模型结果里的命中一律保留，避免因模型漏答造成漏报。
- 模型返回合法但空的 findings 数组时，先追加纠正提示重试一次；仍为空则
  视为本次复核失败，不删除任何命中，下轮再试。
- 单条审计最多复核 ``max_findings_per_audit`` 条（按严重度优先），其余
  命中原样保留，避免超大报告无限消耗 token。
- 每轮只处理 ``details.llm_review`` 为空或此前失败且未超过重试上限的审计，
  复核完成后打标，不会重复消耗模型调用。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import Integer, and_, case, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONPATH

from src.core.config import AuditLLMReviewConfig, get_settings
from src.core.database import AsyncSessionLocal
from src.models.orm import SecurityAudit, Skill, SkillVersion
from src.security.audit_review.apply import ReviewApplication
from src.security.audit_review.context import ContextConfig, FileContentCache
from src.security.audit_review.llm import call_llm_json, post_chat
from src.security.audit_review.log import logger
from src.security.audit_review.persistence import (
    mark_skipped,
    persist,
    persist_partial,
    record_failure,
)
from src.security.audit_review.prompt import SYSTEM_PROMPT
from src.security.audit_review.queue import (
    _RateLimiter,
    next_window_start,
    parse_hhmm,
    resolve_window,
)
from src.security.audit_review.remote_raw import RemoteTokens
from src.security.audit_review.review import (
    ReviewDeadlineError,
    build_partial_meta,
    review_one,
)


class AuditLLMReviewScheduler:
    """每天固定窗口内复核未复核审计的后台任务。

    循环结构天然防重入：一轮 ``run_once`` 结束后才进入下一轮；窗口结束或
    没有待复核记录时，直接睡到下一个窗口开始。
    """

    def __init__(self, config: AuditLLMReviewConfig):
        self._config = config
        self._api_key = _resolve_review_api_key(config)
        self._context_config = ContextConfig(
            mode=config.context_mode,
            window_lines=config.window_lines,
            window_header_lines=config.window_header_lines,
            window_max_ranges=config.window_max_ranges,
            window_gap=config.window_gap,
            full_file_max_bytes=config.full_file_max_bytes,
            max_file_bytes=config.max_file_bytes,
            max_context_chars=config.max_context_chars,
            repo_cache_root=config.repo_cache_root,
            git_timeout=config.git_timeout,
            remote_fetch_enabled=config.remote_fetch_enabled,
            remote_fetch_timeout=config.remote_fetch_timeout,
            remote_fetch_max_bytes=config.remote_fetch_max_bytes,
            remote_fetch_max_files_per_audit=config.remote_fetch_max_files_per_audit,
        )
        self._remote_tokens = RemoteTokens(
            github=config.github_token,
            gitcode=config.gitcode_token,
            gitlab=config.gitlab_token,
            gitee=config.gitee_token,
        )
        self._file_cache = FileContentCache(config.context_cache_max_bytes)
        self._running = False
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _check_window_args(self) -> tuple[tuple[int, int], tuple[int, int]]:
        start = parse_hhmm(self._config.start_time)
        end = parse_hhmm(self._config.end_time)
        if start >= end:
            raise ValueError(
                "audit_llm_review.start_time must be earlier than end_time, "
                f"got {self._config.start_time!r} - {self._config.end_time!r}"
            )
        return start, end

    async def start(self) -> None:
        if self._running:
            return
        self._check_window_args()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "scheduler started (window=%s-%s, model=%s, concurrency=%d, "
            "max_findings_per_audit=%d, context_mode=%s)",
            self._config.start_time,
            self._config.end_time,
            self._config.model,
            self._config.concurrency,
            self._config.max_findings_per_audit,
            self._config.context_mode,
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
        logger.info("scheduler stopped")

    async def _loop(self) -> None:
        start, end = self._check_window_args()
        while self._running:
            now = datetime.now()
            window = resolve_window(now, start, end)
            if window is None:
                target = next_window_start(now, start)
                logger.info(
                    "Next review window starts at %s (in %.0fs)",
                    target,
                    max(0.0, (target - now).total_seconds()),
                )
                await self._sleep_until(target)
                continue

            _, end_at = window
            try:
                stats = await self.run_once(deadline=end_at)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 数据库等非模型侧异常：窗口内稍后重试，不直接放弃整晚
                logger.exception("Scheduled review run failed")
                await asyncio.sleep(60)
                continue

            if stats.get("stopped") == "no_credentials":
                # 配置缺失时不空转：直接睡到下一个窗口（配置在进程启动时读取）
                target = next_window_start(datetime.now(), start)
                logger.info("Next review window starts at %s", target)
                await self._sleep_until(target)
                continue
            if stats.get("stopped") in {"deadline", "no_work", "error"}:
                target = next_window_start(datetime.now(), start)
                logger.info("Next review window starts at %s", target)
                await self._sleep_until(target)
                continue
            await asyncio.sleep(60)

    async def _sleep_until(self, target: datetime) -> None:
        while self._running:
            remaining = (target - datetime.now()).total_seconds()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 3600.0))

    # ------------------------------------------------------------------
    # 单轮执行
    # ------------------------------------------------------------------

    async def run_once(self, deadline: datetime | None = None) -> dict[str, Any]:
        """处理待复核审计直到窗口结束或没有待复核记录。"""
        config = self._config
        if not config.base_url or not self._api_key:
            logger.warning("skipped: audit_llm_review.base_url/api_key is not configured")
            return {"reviewed": 0, "failed": 0, "skipped": 0, "stopped": "no_credentials"}

        limiter = _RateLimiter(rpm=config.rpm, tpm=config.tpm)
        # 每次多取一些，减少 JSONB 全表过滤的查询次数；实际写库仍按并发度推进
        page_size = max(10, config.concurrency * 10)
        reviewed = failed = skipped = partial = 0
        consecutive_failures = 0
        # 供应商整体不可用时快速熔断，避免把整个待复核队列都消耗成失败重试
        failure_threshold = max(3, config.concurrency * 3)
        stopped = "no_work"
        started_at = time.monotonic()
        # 一轮 pass 内用 keyset 游标保证每条审计只处理一次，避免一条超大审计
        # 挤占其他审计；一轮扫完后如果还有剩余且窗口未结束，立即开始下一轮，
        # 因此队列空闲时同一条审计可以在当晚连续推进多个 200 条。
        after: tuple[int, int, int, datetime, Any] | None = None
        pass_index = 1
        pass_base = 0

        timeout = httpx.Timeout(config.timeout, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            while True:
                if deadline is not None and datetime.now() >= deadline:
                    stopped = "deadline"
                    break
                rows = await self._fetch_pending(page_size, after=after)
                if not rows:
                    if reviewed + partial > pass_base:
                        # 本轮有实际推进，重置游标开始下一轮（round-robin）
                        logger.info(
                            "pass %d complete: reviewed=%d partial=%d; starting next pass",
                            pass_index,
                            reviewed,
                            partial,
                        )
                        pass_index += 1
                        pass_base = reviewed + partial
                        after = None
                        continue
                    stopped = "no_work"
                    break
                after = (
                    int(rows[-1].get("community_priority") or 0),
                    int(rows[-1].get("risk_rank") or 0),
                    int(rows[-1].get("issue_count") or 0),
                    rows[-1]["audited_at"],
                    rows[-1]["id"],
                )

                semaphore = asyncio.Semaphore(max(1, config.concurrency))

                async def _run(
                    row: dict[str, Any],
                    _semaphore: asyncio.Semaphore = semaphore,
                ) -> str:
                    nonlocal reviewed, failed, skipped, partial, consecutive_failures
                    async with _semaphore:
                        if deadline is not None and datetime.now() >= deadline:
                            return "deadline"
                        try:
                            outcome = await self._review_one(
                                client, limiter, row, deadline=deadline
                            )
                        except ReviewDeadlineError:
                            return "deadline"
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.exception("Unexpected review error for audit %s", row.get("id"))
                            outcome = "failed"
                        if outcome in {"reviewed", "partial"}:
                            consecutive_failures = 0
                            if outcome == "reviewed":
                                reviewed += 1
                            else:
                                partial += 1
                        elif outcome == "failed":
                            failed += 1
                            consecutive_failures += 1
                        elif outcome == "skipped":
                            skipped += 1
                        return outcome

                outcomes = await asyncio.gather(*(_run(row) for row in rows))
                if outcomes and all(outcome == "deadline" for outcome in outcomes):
                    stopped = "deadline"
                    break
                if consecutive_failures >= failure_threshold:
                    logger.error(
                        "circuit breaker tripped after %d consecutive failures; stopping this run",
                        consecutive_failures,
                    )
                    stopped = "error"
                    break
                logger.info(
                    "progress: reviewed=%d partial=%d failed=%d skipped=%d elapsed=%.0fs",
                    reviewed,
                    partial,
                    failed,
                    skipped,
                    time.monotonic() - started_at,
                )

        logger.info(
            "run finished: reviewed=%d partial=%d failed=%d skipped=%d duration=%.0fs stopped=%s",
            reviewed,
            partial,
            failed,
            skipped,
            time.monotonic() - started_at,
            stopped,
        )
        return {
            "reviewed": reviewed,
            "partial": partial,
            "failed": failed,
            "skipped": skipped,
            "stopped": stopped,
        }

    # ------------------------------------------------------------------
    # 待复核查询
    # ------------------------------------------------------------------

    async def _fetch_pending(
        self,
        limit: int,
        after: tuple[int, int, int, datetime, Any] | None = None,
    ) -> list[dict[str, Any]]:
        review = SecurityAudit.details["llm_review"]
        # 排序优先级：社区 SIG 绝对优先 -> 风险等级高的优先 -> 命中数少的优先
        # -> 入库时间早的优先。keyset 游标必须是五元组，否则翻页会重复或漏记录。
        issue_count = func.jsonb_array_length(
            SecurityAudit.details["skillspector_report"]["issues"]
        )
        community_priority = case(
            (
                func.coalesce(Skill.platform, SkillVersion.platform) == "community",
                1,
            ),
            else_=0,
        )
        risk_rank = case(
            (SecurityAudit.risk_level == "critical", 4),
            (SecurityAudit.risk_level == "high", 3),
            (SecurityAudit.risk_level == "medium", 2),
            (SecurityAudit.risk_level == "low", 1),
            else_=0,
        )
        conditions = [
            SecurityAudit.audit_type.like("%skillspector%"),
            func.jsonb_path_exists(
                SecurityAudit.details,
                cast(
                    literal("$.skillspector_report.issues[*]"),
                    JSONPATH,
                ),
            ),
            or_(
                review.is_(None),
                review["status"].as_string() == "partial",
                and_(
                    review["status"].as_string() == "failed",
                    cast(review["attempts"].as_string(), Integer) < self._config.max_attempts,
                ),
            ),
            or_(Skill.id.is_not(None), SkillVersion.id.is_not(None)),
        ]
        if after is not None:
            after_priority, after_risk, after_count, after_at, after_id = after
            conditions.append(
                or_(
                    community_priority < after_priority,
                    and_(
                        community_priority == after_priority,
                        or_(
                            risk_rank < after_risk,
                            and_(
                                risk_rank == after_risk,
                                or_(
                                    issue_count > after_count,
                                    and_(
                                        issue_count == after_count,
                                        or_(
                                            SecurityAudit.audited_at > after_at,
                                            and_(
                                                SecurityAudit.audited_at == after_at,
                                                SecurityAudit.id > after_id,
                                            ),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
            )
        stmt = (
            select(
                SecurityAudit.id,
                SecurityAudit.audited_at,
                SecurityAudit.resource_id,
                SecurityAudit.risk_level,
                SecurityAudit.risk_signals,
                SecurityAudit.details,
                SecurityAudit.commit_id.label("audit_commit_id"),
                community_priority.label("community_priority"),
                risk_rank.label("risk_rank"),
                issue_count.label("issue_count"),
                Skill.name.label("skill_name"),
                Skill.description.label("skill_description"),
                Skill.content.label("skill_content"),
                Skill.skill_id.label("skill_skill_id"),
                Skill.source.label("skill_source"),
                Skill.source_url.label("skill_source_url"),
                Skill.repo_url.label("skill_repo_url"),
                Skill.commit_id.label("skill_commit_id"),
                SkillVersion.name.label("version_name"),
                SkillVersion.description.label("version_description"),
                SkillVersion.content.label("version_content"),
                SkillVersion.skill_id.label("version_skill_id"),
                SkillVersion.source.label("version_source"),
                SkillVersion.source_url.label("version_source_url"),
                SkillVersion.repo_url.label("version_repo_url"),
                SkillVersion.commit_id.label("version_commit_id"),
            )
            .outerjoin(Skill, Skill.id == SecurityAudit.resource_id)
            .outerjoin(SkillVersion, SkillVersion.id == SecurityAudit.resource_id)
            .where(*conditions)
            .order_by(
                community_priority.desc(),
                risk_rank.desc(),
                issue_count.asc(),
                SecurityAudit.audited_at.asc(),
                SecurityAudit.id.asc(),
            )
            .limit(limit)
        )
        async with AsyncSessionLocal() as session:
            result = await session.execute(stmt)
            return [dict(row._mapping) for row in result]

    # ------------------------------------------------------------------
    # 单条审计

    async def _review_one(
        self,
        client: httpx.AsyncClient,
        limiter: _RateLimiter,
        row: dict[str, Any],
        deadline: datetime | None = None,
    ) -> str:
        return await review_one(self, client, limiter, row, deadline=deadline)

    def _build_partial_meta(
        self,
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
        return build_partial_meta(
            self._config,
            issues_count=issues_count,
            reviewed_count=reviewed_count,
            false_positive_indices=false_positive_indices,
            false_positive_reasons=false_positive_reasons,
            false_positive_confidences=false_positive_confidences,
            low_confidence_indices=low_confidence_indices,
            low_confidence_reasons=low_confidence_reasons,
            started_at=started_at,
            context_stats=context_stats,
        )

    async def _call_llm_json(
        self,
        client: httpx.AsyncClient,
        limiter: _RateLimiter,
        prompt: str,
        expected: int,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> list[dict[str, Any]]:
        return await call_llm_json(
            client,
            limiter,
            prompt,
            expected,
            post=self._post_chat,
            system_prompt=system_prompt,
        )

    async def _post_chat(
        self,
        client: httpx.AsyncClient,
        limiter: _RateLimiter,
        messages: list[dict[str, str]],
    ) -> str:
        return await post_chat(
            client,
            limiter,
            config=self._config,
            api_key=self._api_key,
            messages=messages,
        )

    async def _persist(
        self,
        row: dict[str, Any],
        reviewed_report: dict[str, Any],
        application: ReviewApplication,
    ) -> str:
        return await persist(row, reviewed_report, application)

    async def _persist_partial(
        self,
        audit_id: Any,
        reviewed_report: dict[str, Any],
        partial_meta: dict[str, Any],
    ) -> str:
        return await persist_partial(audit_id, reviewed_report, partial_meta)

    async def _record_failure(
        self,
        audit_id: Any,
        error: str,
        context_stats: dict[str, Any] | None = None,
        reviewed_count: int | None = None,
    ) -> None:
        await record_failure(
            audit_id,
            error,
            model=self._config.model,
            context_mode=self._config.context_mode,
            context_stats=context_stats,
            reviewed_count=reviewed_count,
        )

    async def _mark_skipped(self, audit_id: Any, reason: str) -> None:
        await mark_skipped(audit_id, reason)


def _resolve_review_api_key(config: AuditLLMReviewConfig) -> str:
    """复核 API Key：优先 audit_llm_review.api_key。

    未单独配置时回退复用 ``model.providers.siliconflow`` 的凭据
    （兼容 skillhub 分支的多 provider 配置）；仍为空则任务自动跳过。
    """
    if config.api_key:
        return config.api_key
    providers = getattr(get_settings().model, "providers", None)
    if not isinstance(providers, dict):
        return ""
    entry = providers.get("siliconflow")
    if isinstance(entry, dict):
        return str(entry.get("api_key") or "")
    return str(getattr(entry, "api_key", "") or "")


async def start_audit_llm_review_scheduler() -> AuditLLMReviewScheduler | None:
    """按配置创建并启动 LLM 复核调度器；未启用时返回 None。"""
    settings = get_settings()
    config = settings.audit_llm_review
    if not config.enabled:
        logger.info("disabled (audit_llm_review.enabled=false)")
        return None
    scheduler = AuditLLMReviewScheduler(config)
    await scheduler.start()
    return scheduler
