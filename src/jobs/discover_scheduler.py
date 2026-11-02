"""后台定时 discover 调度器。

服务启动时立即执行一轮 discover，并按 DiscoverSchedulerConfig
（config.yaml 的 discover_scheduler 段）在每天或每周的固定时刻再触发：
读取 openEuler-skills catalog 中的仓库配置，逐仓库执行
SkillManager.discover_configured_skill_repository。

调度基于纯 asyncio（跑完一轮 sleep 到下一个触发点），与 SkillspectorCollector
的后台任务模式一致，不引入额外依赖。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.skills.skill_manager import SkillManager
from src.skills.discover_requests import build_configured_discover_requests
from src.skills.discover_result import (
    build_discover_result_document,
    classify_discover_result,
    collect_skill_stats,
    save_discover_result_file,
)
from src.skills.config import load_contributor_profiles, sync_openEuler_skills_repo
from src.core.config import DiscoverSchedulerConfig
from src.core.database import AsyncSessionLocal
from src.models.repository import ContributorRepository, SkillRepoRepository, SkillRepository

logger = logging.getLogger(__name__)
LOG_PREFIX = "[Discover]"

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# 北京时间（UTC+8）：调度配置与计算统一按 UTC，日志换算成北京时间展示
BEIJING_TZ = timezone(timedelta(hours=8))


# security_audits 中「异步检测仍在进行」的判定条件等共享定义见
# skillcrawler/main.py（collect_skill_stats）；结果 JSON 的 counters /
# skill_stats / remediation_run 构建与 <key>_note 说明同样由该模块提供，
# CLI 与自动调度共用同一实现，口径完全一致。


def _beijing_hhmm(time_str: str) -> str:
    """把配置里的 UTC HH:MM 换算成北京时间 HH:MM；解析失败时原样返回。"""
    try:
        hour, minute = _parse_hhmm(time_str)
    except ValueError:
        return time_str
    return f"{(hour + 8) % 24:02d}:{minute:02d}"


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time (expected HH:MM): {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time (expected HH:MM): {value!r}")
    return hour, minute


def compute_next_run(
    now: datetime,
    interval: str,
    hour: int,
    minute: int,
    weekday: str,
) -> datetime:
    """计算下一次触发时刻（UTC）。

    daily：今天已过触发点则取明天同一时刻；
    weekly：取下一个指定 weekday 的触发时刻（今天已过则顺延一周）。
    """
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if interval == "weekly":
        target = WEEKDAYS.index(weekday)
        days_ahead = (target - now.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


class DiscoverScheduler:
    """启动即跑一轮、之后定时触发 discover 流程的后台任务。

    循环结构天然防重入：每轮 run_discover_once 执行完毕后才计算下一个
    触发点，克隆扫描耗时较长时下一轮自动顺延，不会叠加执行。
    """

    def __init__(self, config: DiscoverSchedulerConfig):
        self._config = config
        self._running = False
        self._task: asyncio.Task[None] | None = None

    def _check_interval_args(self) -> str:
        """校验 interval / weekday 配置，返回规范化后的 interval。"""
        interval = self._config.interval.strip().lower()
        if interval not in ("daily", "weekly"):
            raise ValueError(f"discover_scheduler.interval must be daily or weekly, got {interval!r}")
        if interval == "weekly" and self._config.weekday.strip().lower() not in WEEKDAYS:
            raise ValueError(f"discover_scheduler.weekday must be one of {WEEKDAYS}, got {self._config.weekday!r}")
        return interval

    async def start(self) -> None:
        if self._running:
            return
        interval = self._check_interval_args()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "%s scheduler started (interval=%s, trigger=(UTC:%s == 北京时间:%s), weekday=%s)",
            LOG_PREFIX,
            interval,
            self._config.time,
            _beijing_hhmm(self._config.time),
            self._config.weekday,
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
        interval = self._config.interval.strip().lower()
        hour, minute = _parse_hhmm(self._config.time)
        weekday = self._config.weekday.strip().lower()

        while self._running:
            # 启动即跑一轮：重启后立即自愈，不必等下个调度点
            #（当天触发点已过时原本要等最长 24h）；单轮异常只记录日志。
            try:
                await self.run_discover_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 单轮整体异常不应终止调度循环，记录后等待下一个调度点
                logger.exception("%s round failed", LOG_PREFIX)
            if not self._running:
                return

            # 触发时刻按 UTC 计算（config.yaml 的 time 为 UTC HH:MM）；
            # 每轮执行完毕后才计算下一个触发点，克隆扫描耗时较长时下一轮自动顺延
            next_run = compute_next_run(
                datetime.now(timezone.utc), interval, hour, minute, weekday,
            )
            wait_seconds = (next_run - datetime.now(timezone.utc)).total_seconds()
            logger.info(
                "%s next round at %s 北京时间 (UTC %s, in %.0fs)",
                LOG_PREFIX,
                next_run.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M"),
                next_run.strftime("%Y-%m-%d %H:%M"),
                wait_seconds,
            )
            try:
                await asyncio.sleep(wait_seconds)
            except asyncio.CancelledError:
                return

    async def run_discover_once(self) -> dict[str, int]:
        """执行一轮 discover（全部配置仓库），返回分类统计。

        每轮记录开始/结束日志与总耗时；每个仓库记录处理耗时，
        结束时汇总平均耗时与最慢的仓库，便于排查慢仓库。
        每轮结果（含每仓明细）写入 result_dir 下的 JSON 文件。
        """
        # catalog 同步是 git 操作、请求构建是全量 YAML 解析，均放线程池执行，
        # 网络挂起或 catalog 较大时不阻塞事件循环（API / 健康检查保持可用）
        repository_path = await asyncio.to_thread(sync_openEuler_skills_repo)
        requests, config_keys = await asyncio.to_thread(
            build_configured_discover_requests,
            repository_path=repository_path,
        )
        run_started = time.monotonic()
        run_started_wall = datetime.now()

        counters = {
            "created": 0,
            "rediscovered": 0,
            "unchanged": 0,
            "removed": 0,
            "no_skill": 0,
            "failed": 0,
        }
        repo_durations: list[tuple[str, float]] = []
        repo_details: list[dict[str, Any]] = []

        async with AsyncSessionLocal() as session:
            manager = SkillManager(
                skill_repository=SkillRepository(session),
                skill_repo_repository=SkillRepoRepository(session),
                contributor_repository=ContributorRepository(session),
                catalog_path=repository_path,
            )
            # 先同步删除 catalog 中已消失的仓库，再开始扫描新增/更新。
            # 失败仅记录日志，不阻断本轮扫描（清理属于自愈，非主流程）。
            try:
                removed_repos = await manager.delete_removed_catalog_repos(requests)
                removed_skills = await manager.delete_removed_catalog_skills(requests)
                if removed_repos or removed_skills:
                    logger.info(
                        "%s synced catalog removals before scan: %d repo(s), %d skill(s) deleted",
                        LOG_PREFIX,
                        removed_repos,
                        removed_skills,
                    )
                else:
                    logger.info("%s no catalog repo/skill removed before scan", LOG_PREFIX)
            except Exception:
                logger.exception("%s catalog removal sync failed", LOG_PREFIX)

            total = len(requests)
            logger.info(
                "%s round started: %d repos from %s (catalog: %s)",
                LOG_PREFIX,
                total,
                ", ".join(config_keys),
                repository_path,
            )
            for index, request in enumerate(requests, start=1):
                label = request.url or "<missing-url>"
                logger.info("%s [%d/%d] scanning %s", LOG_PREFIX, index, total, label)
                repo_started = time.monotonic()
                try:
                    result = await manager.discover_configured_skill_repository(request)
                except Exception as exc:
                    elapsed = time.monotonic() - repo_started
                    repo_durations.append((label, elapsed))
                    counters["failed"] += 1
                    repo_details.append(
                        {
                            "url": label,
                            "outcome": "failed",
                            "skill_num": 0,
                            "duration_seconds": round(elapsed, 3),
                            "error": str(exc),
                        }
                    )
                    logger.exception(
                        "%s failed for %s after %.1fs", LOG_PREFIX, label, elapsed
                    )
                    continue

                elapsed = time.monotonic() - repo_started
                repo_durations.append((label, elapsed))
                outcome = classify_discover_result(result)
                counters[outcome] += 1
                repo_details.append(
                    {
                        "url": label,
                        "outcome": outcome,
                        "skill_num": getattr(result, "skill_num", 0),
                        "duration_seconds": round(elapsed, 3),
                    }
                )
                logger.info(
                    "%s [%d/%d] %s: %s (%d skills, took %.1fs)",
                    LOG_PREFIX,
                    index,
                    total,
                    outcome,
                    label,
                    getattr(result, "skill_num", 0),
                    elapsed,
                )

            # 扫描结束后同步 catalog 各 platform 的贡献者信息（name/description/
            # git_profile）：覆盖 commit 未变被跳过的仓库，以及已登记但尚未
            # 扫描出 skill 的组织；skill_count 始终以扫描结果为准
            profile_count = await self._sync_contributor_profiles(session, repository_path)
            logger.info(
                "%s contributor profiles synced from catalog: %d entries",
                LOG_PREFIX, profile_count,
            )

        # 补跑衍生字段缺口（security 风险分 / category 分类 / embedding 向量）：
        # 本轮新入库或中断留下的缺口行在此补齐，使其立即可用；
        # 各任务自持pg_advisory_lock 互斥（embedding 的锁另与全量重建脚本互斥），单任务
        # 失败/跳过只记录在该任务的统计里。
        # 失败不重试，漏掉的行由下轮 discover 或 API 启动时的补齐自愈。
        remediation_run: dict[str, Any] | None = None
        try:
            from src.jobs.remediation import run_remediations

            remediation_run = await run_remediations()
            logger.info("%s remediation after discover: %s", LOG_PREFIX, remediation_run)
        except Exception:
            logger.exception("%s remediation after discover failed", LOG_PREFIX)

        # skill 级统计：分类分布 / 安全检测结果 / 索引创建状态
        skill_stats: dict[str, Any] | None = None
        try:
            async with AsyncSessionLocal() as session:
                skill_stats = await collect_skill_stats(session)
        except Exception:
            logger.exception("%s failed to collect skill stats after discover", LOG_PREFIX)

        run_elapsed = time.monotonic() - run_started
        avg_elapsed = run_elapsed / total if total else 0.0
        slowest = sorted(repo_durations, key=lambda item: item[1], reverse=True)[:3]
        slowest_text = (
            ", ".join(f"{label}={duration:.1f}s" for label, duration in slowest) or "n/a"
        )
        logger.info(
            "%s round finished: %d repos, %s, total %.1fs, "
            "avg %.1fs/repo, slowest: %s",
            LOG_PREFIX,
            total,
            counters,
            run_elapsed,
            avg_elapsed,
            slowest_text,
        )

        # 结果文档构建与保存由 skillcrawler.main 提供（CLI discover 共用）
        save_discover_result_file(
            build_discover_result_document(
                started_at=run_started_wall,
                run_started=run_started,
                total_repos=total,
                config_keys=config_keys,
                counters=counters,
                repo_details=repo_details,
                remediation_run=remediation_run,
                skill_stats=skill_stats,
            ),
            result_dir=self._config.result_dir,
        )
        return counters

    @staticmethod
    async def _sync_contributor_profiles(
        session: Any, repository_path: Path
    ) -> int:
        """把 openEuler-skills catalog 各 platform 的贡献者信息注入 contributors 表。

        读取 community/<sig>/、enterprise/<org>/、personal/<author>/ 下的
        skill.yaml（模板规范见 templates/*-skill-spec.yaml），将 name/
        description/git_profile/website 等 profile 字段 upsert 进
        contributors。
        失败只记录 warning，不影响扫描主流程；skill_count 不在同步范围
        （以扫描结果为准，新插入行默认 0）。
        """
        try:
            profiles = await asyncio.to_thread(
                load_contributor_profiles, repository_path,
            )
        except Exception:
            logger.warning(
                "[Discover] Failed to load contributor profiles from %s", repository_path, exc_info=True
            )
            return 0

        repo = ContributorRepository(session)
        synced = 0
        try:
            for profile in profiles:
                if not profile["source"]:
                    # 未登记任何仓库，无法确定 (source, author) 键，跳过
                    continue
                await repo.upsert(
                    source=profile["source"],
                    author=profile["author"],
                    platform=profile["platform"],
                    name=profile["name"],
                    description=profile["description"],
                    git_profile=profile["git_profile"],
                    website=profile["website"],
                    repo_url=profile["repo_url"],
                    commit=False,
                )
                synced += 1
            await session.commit()
        except Exception:
            await session.rollback()
            logger.warning("[Discover] Failed to sync contributor profiles", exc_info=True)
            return 0
        return synced


async def start_discover_scheduler() -> DiscoverScheduler | None:
    """按配置创建并启动 discover 调度器；未启用时返回 None。"""
    from src.core.config import get_settings

    settings = get_settings()
    if not settings.discover_scheduler.enabled:
        logger.info("[Discover] DiscoverScheduler disabled (discover_scheduler.enabled=false)")
        return None

    scheduler = DiscoverScheduler(settings.discover_scheduler)
    await scheduler.start()
    return scheduler
