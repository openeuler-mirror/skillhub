"""license 三态标记定时任务（唯一负责 license 检查与写值的入口）。

业务规则
--------
``skills`` / ``skill_versions`` 的 ``license_compliant`` 三态：

- ``1``：已检查且合规（判定状态 allowed）
- ``0``：已检查但不合规（restricted/black/missing/unknown）
- ``NULL``：尚未检查，或本轮判定失败（清单不可用、仓库不可读等）

判定以 SKILL.md 声明为准；声明缺失，或声明是无法解析的描述性占位时才
回退 LICENSE 文件。本任务**只标记、不删除**任何数据；discover 不参与
检查与写值。

每轮流程
--------
1. 获取 PostgreSQL advisory lock，多副本下只允许一个实例执行；
2. 构建 checker（加载准入清单；不可用则整轮跳过，不写 0）；
3. 一次性加载全库 skills / skill_versions（含 repo 的 url/local_path）；
4. 按仓库分组并发判定：同仓库内按 ``(skill_dir, declared)`` 复用结论，
   同一个 skill 的多个 tag 版本只判定一次；
5. 只把结论变化的行写回（1/0），判定失败的行保持原值；
6. 可见性变化后重算 contributor.skill_count（口径与列表一致）；
7. 按 ``repo.remote_cache_keep_days`` 回收本轮未使用的远程/_local 缓存。

调度
----
worker 在 discover 首轮完成后创建调度器：启动即跑一轮（覆盖存量/新入库
数据），之后每天 ``license_check.start_time``（按 timezone 解释）触发。
本任务没有固定的结束时间窗口，跑完为止；耗时主要取决于唯一 skill 目录数
和缺失本地 clone 的仓库数量。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from sqlalchemy import select, text, update

from src.core.config import LicenseCheckConfig, get_settings
from src.core.database import AsyncSessionLocal
from src.core.scheduling import next_daily, now_in, parse_hhmm, resolve_timezone
from src.models.orm import Skill, SkillRepoModel, SkillVersion
from src.models.repository import ContributorRepository
from src.skills.license.gate_engine import normalize_expression
from src.skills.license.runtime import build_checker, verdict_to_flag
from src.skills.license.scanner import LicenseChecker, derive_skill_dir, normalize_repo_key

logger = logging.getLogger(__name__)
LOG_PREFIX = "[LicenseCheck]"
_LOCK_KEY = 761124002


class LicenseCheckScheduler:
    """license 全量复核调度器（启动即跑一轮，之后每日触发）。

    生命周期由 worker 管理：

    - ``start_license_check_scheduler`` 在 discover 首轮完成后调用 ``start``；
    - worker 关闭时调用 ``stop`` 取消后台任务。

    ``first_round_done`` 在本轮（无论成功/失败）结束后置位，便于需要
    “license 结论先就绪”的后续任务等待，但当前 worker 未阻塞 LLM 复核。
    """

    def __init__(self, config: LicenseCheckConfig):
        self._config = config
        # 所有定时计算都使用配置时区，避免依赖容器 TZ（线上容器通常为 UTC）
        self._tz = resolve_timezone(config.timezone)
        self._running = False
        self._task: asyncio.Task[None] | None = None
        # 首轮完成信号：worker/测试可等待，用于确认存量数据已完成一轮判定
        self.first_round_done = asyncio.Event()

    def _now(self) -> datetime:
        """当前时刻（带配置时区），用于计算下一次触发时间。"""
        return now_in(self._tz)

    def _check_start_args(self) -> tuple[int, int]:
        """校验并解析 HH:MM 配置；非法配置在启动阶段直接抛错。"""
        return parse_hhmm(self._config.start_time)

    async def start(self) -> None:
        """启动后台循环；重复调用幂等。"""
        if self._running:
            return
        self._check_start_args()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "%s scheduler started (start_time=%s timezone=%s)",
            LOG_PREFIX,
            self._config.start_time,
            self._config.timezone,
        )

    async def stop(self) -> None:
        """停止后台循环；取消正在执行的一轮（已提交的批次保留）。"""
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
        """先立即执行一轮，之后每天在 start_time 触发。"""
        start = self._check_start_args()
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 单轮失败不退出调度器：记录后等下一次触发
                logger.exception("%s round failed", LOG_PREFIX)
            finally:
                # 无论成功失败都置位，避免等待方永久阻塞
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

    async def run_once(self) -> dict[str, Any]:
        """执行一轮全量复核（全库 skills + skill_versions），返回本轮统计。

        锁语义：使用 PostgreSQL **会话级** advisory lock，保证多副本部署时
        同一时刻只有一个实例在跑全量复核（避免重复 clone / 重复写库）。

        - 抢锁失败（``lock_busy``）：直接返回，不视为错误，等下一轮；
        - 抢锁成功：执行 ``_run_locked``，无论成功/异常都在 finally 解锁。

        之所以用单独的 ``lock_session``：会话级锁必须由持有它的连接释放，
        不能混用业务 session（业务 session 会频繁 commit，但会话锁不随
        事务提交释放）。

        返回值中的 ``stopped`` 表示本轮提前终止的原因：
        ``lock_busy``（锁被占用）/ ``access_list_unavailable``（清单不可用）。
        """
        started = time.monotonic()
        async with AsyncSessionLocal() as lock_session:
            locked = await lock_session.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": _LOCK_KEY}
            )
            if not locked.scalar():
                logger.info("%s skipped: lock held by another task", LOG_PREFIX)
                return {"stopped": "lock_busy"}
            try:
                return await self._run_locked(started)
            finally:
                # 即使 _run_locked 抛异常也必须解锁，否则下一轮永远拿不到锁
                await lock_session.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY}
                )

    async def _run_locked(self, started: float) -> dict[str, Any]:
        """在已持有 advisory lock 的前提下完成：建 checker -> 取数 -> 判定
        -> 落库 -> 回收缓存。

        :param started: ``run_once`` 记录的开始时间（``time.monotonic()``），
            用于在 round 结束时计算总耗时
        """
        settings = get_settings()
        # 加载准入清单是阻塞 I/O（可能走网络），放线程池避免卡住事件循环。
        # required=False：清单不可用时返回 None 而不是抛错，让本轮优雅跳过。
        checker = await asyncio.to_thread(build_checker, settings, required=False)
        if checker is None:
            # 清单不可用时不写 0：保持原值，等待下一轮重试
            logger.error("%s skipped: license 准入清单不可用", LOG_PREFIX)
            return {"stopped": "access_list_unavailable"}

        # 全量加载：不做 license_compliant IS NULL 过滤，因为合规结论可能
        # 随准入清单更新而变化，必须每天重新判断所有记录。
        rows = await self._load_records()
        if not rows:
            logger.info("%s nothing to check", LOG_PREFIX)
            try:
                # 数据库为空时也按保留期回收遗留缓存
                await self._prune_caches(checker, [])
            finally:
                await asyncio.to_thread(checker.close)
            return {"scanned": 0, "checked": 0, "compliant": 0, "non_compliant": 0, "errors": 0}

        try:
            results = await self._check_rows(checker, rows)
            counters = await self._persist(results)
            await self._prune_caches(checker, rows)
        finally:
            # 清理本地 clone 物化出的临时目录；持久化缓存由 prune 方法回收
            await asyncio.to_thread(checker.close)

        duration = round(time.monotonic() - started, 1)
        summary = {
            "scanned": len(rows),  # 本轮读取的行数（skills + skill_versions）
            "checked": counters["compliant"] + counters["non_compliant"],  # 得到确定结论的行数
            "compliant": counters["compliant"],  # 结论为 1 的行数
            "non_compliant": counters["non_compliant"],  # 结论为 0 的行数
            "errors": counters["errors"],  # 判定失败（保持原值）的行数
            "duration_seconds": duration,
        }
        logger.info(
            "%s round finished: scanned=%d compliant=%d non_compliant=%d errors=%d duration=%.1fs",
            LOG_PREFIX,
            summary["scanned"],
            summary["compliant"],
            summary["non_compliant"],
            summary["errors"],
            duration,
        )
        return summary

    async def _load_records(self) -> list[dict[str, Any]]:
        """全量加载 skills / skill_versions 行，供本轮复核。

        每行包含判定所需的全部字段，避免判定阶段再逐条查库：

        - ``id``：回写目标主键；
        - ``table``：区分回写 ``skills`` 还是 ``skill_versions``；
        - ``skill_id``：仅用于日志定位（版本行的业务 ID）；
        - ``source_url``：经 ``derive_skill_dir`` 推导 skill 相对目录；
        - ``extra_metadata``：读 ``license`` 声明（SKILL.md frontmatter 解析结果）；
        - ``license_compliant``：当前值，用于“结论未变化则不写库”；
        - ``url`` / ``local_path``：仓库分组键与本地 clone 定位。

        注意：这里刻意 **不** 过滤 ``license_compliant IS NULL``。每日全量
        复核会让已有结论随准入清单更新而自动修正；只补 NULL 会漏掉“准入
        清单新增黑名单后存量 1 需要变 0”的场景。
        """
        rows: list[dict[str, Any]] = []
        # 两个表分两次查询（而不是 UNION），字段语义一致但模型不同，
        # 分开查更易读，也避免 UNION 时类型对齐的额外处理。
        async with AsyncSessionLocal() as session:
            skill_stmt = (
                select(
                    Skill.id,
                    Skill.skill_id,
                    Skill.source_url,
                    Skill.extra_metadata,
                    Skill.license_compliant,
                    SkillRepoModel.url,
                    SkillRepoModel.local_path,
                )
                .join(SkillRepoModel, Skill.skill_repo_id == SkillRepoModel.id)
            )
            for row in (await session.execute(skill_stmt)).all():
                rows.append({**dict(row._mapping), "table": "skills"})

            version_stmt = (
                select(
                    SkillVersion.id,
                    SkillVersion.skill_id,
                    SkillVersion.source_url,
                    SkillVersion.extra_metadata,
                    SkillVersion.license_compliant,
                    SkillRepoModel.url,
                    SkillRepoModel.local_path,
                )
                .join(SkillRepoModel, SkillVersion.skill_repo_id == SkillRepoModel.id)
            )
            for row in (await session.execute(version_stmt)).all():
                rows.append({**dict(row._mapping), "table": "skill_versions"})
        return rows

    async def _check_rows(
        self,
        checker: LicenseChecker,
        rows: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], int | None]]:
        """按仓库聚合并并发判定；返回 ``(原始行, flag)`` 列表。

        三级缓存/并发策略（全量复核的主要性能来源）：

        1. **仓库级并发**：以 ``(repo_url, local_path)`` 分组，不同仓库并行，
           由 ``remote_concurrency`` 信号量限流。同一个仓库内串行，既避免
           同一 repo 并发 clone 冲突，也复用 checker 内的目录级 LICENSE 缓存；
        2. **技能级结论复用**：同仓库内以 ``(skill_dir, declared)`` 为键缓存
           flag。同一 skill 的多个 tag 版本（skill_versions 多行）通常共享
           同一目录与声明，只判定一次；
        3. **目录级文件缓存**：由 ``LicenseScanner`` 内部按目录缓存 LICENSE
           文件列表（含负结果），消除同仓库多 skill 反复 listing 父目录的开销。

        异常处理：单条判定抛出异常时记 ``None``（未检查），调用方保持数据库
        原值；不写 0，避免把基础设施问题误判为 license 不合规。
        """
        # 按仓库分组：url 相同但 local_path 不同也应视为不同来源（例如本地
        # clone 更换目录后，旧行仍指向老路径），所以两者一起作为 key。
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            key = (row.get("url") or "", row.get("local_path") or "")
            groups.setdefault(key, []).append(row)

        # 仓库级并发上限；至少为 1，防止配置成 0 导致 gather 永久挂起
        semaphore = asyncio.Semaphore(max(1, self._config.repo.remote_concurrency))

        async def check_group(key: tuple[str, str], items: list[dict[str, Any]]):
            """判定单个仓库内的所有行；semaphore 覆盖整个仓库的处理。"""
            repo_url, local_path = key
            # 仓库内局部缓存：key=(skill_dir, 归一化 declared)，value=1/0/None
            flags: dict[tuple[str, str], int | None] = {}
            async with semaphore:
                results = []
                for item in items:
                    # SKILL.md frontmatter 的 license 声明（discover 解析后存
                    # 在 extra_metadata.license）。YAML 允许 list 写法
                    # （如 license: [MIT, Apache-2.0]），不能直接用作 dict
                    # key；先用 normalize_expression 归一化成可哈希字符串，
                    # 语义等价的声明也能命中同一缓存。
                    metadata = item.get("extra_metadata")
                    declared = (
                        metadata.get("license") if isinstance(metadata, dict) else None
                    )
                    skill_dir = derive_skill_dir(item.get("source_url") or "")
                    cache_key = (skill_dir, normalize_expression(declared))
                    if cache_key in flags:
                        # 同一 skill 的其他版本/同名声明直接复用结论
                        results.append((item, flags[cache_key]))
                        continue
                    try:
                        # git/文件读取是阻塞调用，放线程池；checker 内部缓存
                        # 是只读+单次 dict 操作，多线程并发安全
                        result = await asyncio.to_thread(
                            checker.check, repo_url, local_path, skill_dir, declared,
                        )
                        flag = verdict_to_flag(result.verdict.status)
                    except Exception:
                        # 未预期异常：按未检查处理（保持原值），不中断整轮
                        logger.exception(
                            "%s check failed: skill=%s repo=%s",
                            LOG_PREFIX,
                            item["skill_id"],
                            repo_url,
                        )
                        flag = None
                    flags[cache_key] = flag
                    results.append((item, flag))
                return results

        results: list[tuple[dict[str, Any], int | None]] = []
        # asyncio.gather 保持传入顺序返回，便于结果与分组稳定对应
        groups_result = await asyncio.gather(
            *(check_group(key, items) for key, items in groups.items())
        )
        for group in groups_result:
            results.extend(group)
        return results

    async def _persist(
        self,
        results: list[tuple[dict[str, Any], int | None]],
    ) -> dict[str, int]:
        """把判定结论写回数据库；只写 1/0，None 与未变化值跳过。

        写入策略：

        - ``flag is None``：判定失败，**不写库**（保留原值，可能是 1/0/NULL）；
        - ``flag == 当前值``：跳过，减少每日全量复核的 WAL/锁开销；
        - 其余按 ``(表, flag)`` 聚合后分批 ``UPDATE ... WHERE id IN (...)``，
          每批独立 commit，避免长事务持有大量行锁。

        contributor 统计自愈：``license_compliant`` 从 1 变 0 会让该 skill 从
        可见集合消失，``contributor.skill_count`` 需要同步下调；反向变化同理。
        写库完成后调用 ``reconcile_skill_counts`` 做一次全量对账（幂等）。
        """
        counters = {"compliant": 0, "non_compliant": 0, "errors": 0}
        # pending: (表名, 目标flag) -> [主键id]
        pending: dict[tuple[str, int], list[Any]] = {}
        for item, flag in results:
            if flag is None:
                # 判定失败：不计入 checked，也不改库
                counters["errors"] += 1
                continue
            counters["compliant" if flag == 1 else "non_compliant"] += 1
            # 结论未变化时不重复写库，降低每日全量复核的写入量
            if item.get("license_compliant") == flag:
                continue
            pending.setdefault((item["table"], flag), []).append(item["id"])

        batch_size = max(1, self._config.batch_size)
        async with AsyncSessionLocal() as session:
            for (table, flag), ids in pending.items():
                model = Skill if table == "skills" else SkillVersion
                for start in range(0, len(ids), batch_size):
                    chunk = ids[start:start + batch_size]
                    await session.execute(
                        update(model)
                        .where(model.id.in_(chunk))
                        .values(license_compliant=flag)
                    )
                    # 每批提交：某批失败会中断后续批次，但前面已提交的保留
                    await session.commit()
            # license 可见性变化会影响 contributor 的口径，落库后自愈聚合统计
            try:
                corrected = await ContributorRepository(session).reconcile_skill_counts(
                    commit=False
                )
                await session.commit()
                if corrected:
                    logger.info("%s contributor skill_count corrected: %d", LOG_PREFIX, corrected)
            except Exception:
                # 统计对账失败不影响 license 结论本身，回滚后记录即可
                await session.rollback()
                logger.exception("%s failed to reconcile contributor counts", LOG_PREFIX)
        return counters

    async def _prune_caches(
        self,
        checker: LicenseChecker,
        rows: list[dict[str, Any]],
    ) -> None:
        """回收本轮未使用的远程仓库缓存与 ``_local`` 物化缓存。

        - ``remote_cache_keep_days <= 0`` 时禁用清理；
        - 本轮仍在扫描的远程仓库 key 会传给 ``prune_remote_cache`` 作为
          active 集合，避免删掉仍在使用、但 mtime 较旧的缓存；
        - ``_local`` 由 checker 内部按本轮实际物化的目录做 active 集合；
        - 清理失败只记录日志，不影响本轮 license 结论与写库结果。
        """
        try:
            keep_days = self._config.repo.remote_cache_keep_days
            if keep_days <= 0:
                return
            active_keys = {
                normalize_repo_key(row.get("url") or "")
                for row in rows
                if row.get("url")
            }
            remote_removed = await asyncio.to_thread(
                checker.prune_remote_cache, keep_days, active_keys
            )
            local_removed = await asyncio.to_thread(
                checker.prune_local_cache, keep_days
            )
        except Exception:
            logger.exception("%s failed to prune license caches", LOG_PREFIX)
            return
        if remote_removed or local_removed:
            logger.info(
                "%s cache pruned: remote=%d local=%d keep_days=%d",
                LOG_PREFIX,
                remote_removed,
                local_removed,
                keep_days,
            )


async def start_license_check_scheduler() -> LicenseCheckScheduler | None:
    """按配置创建并启动 license 复核调度器；未启用时返回 None。

    由 worker 在 discover 首轮完成后调用：此时新入库数据已就绪，启动即跑
    的一轮可以覆盖存量与新数据。返回的调度器由 worker 持有，关闭时调用
    ``stop``。
    """
    config = get_settings().license_check
    if not config.enabled:
        logger.info("%s disabled (license_check.enabled=false)", LOG_PREFIX)
        return None
    scheduler = LicenseCheckScheduler(config)
    await scheduler.start()
    return scheduler
