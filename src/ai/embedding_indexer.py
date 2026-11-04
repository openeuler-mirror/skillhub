"""embedding 索引共享核心：全量重建与增量补嵌入。

供两处调用：
- scripts/reindex_embeddings.py   手动全量或单 Skill 重建（独立进程）
- DiscoverScheduler               每轮 discover 结束后增量补 NULL 行

跨进程互斥：pg_advisory_lock（会话断开自动释放，无需清理）。
- 全量运行中，增量 try_lock 失败 → 跳过本轮（全量本身覆盖一切，含 NULL 行）
- 增量运行中，全量 fail-fast 提示稍后重试
- 同一任务内部全量结束后的 NULL 尾扫复用已持有的锁

批次三段式短事务（避免 embedding HTTP 120s 超时期间占用连接池）：
  ① 短会话读 (skill_id, 文本字段) → 关闭
  ② 无 DB 连接调 embedding 服务批量编码
  ③ 短会话按 skill_id 排序批量 UPDATE → commit → 关闭（排序写消除并发批间死锁）
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import text

from src.ai.embedding import generate_embeddings, prepare_skill_text
from src.core.config import get_settings
from src.core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)
# 共享内部（批次编码/写库）与全量重建流程的日志前缀；
# 补齐入口 embed_missing 全程用 REMEDIATION_LOG_PREFIX（与安全/分类补齐同款）
LOG_PREFIX = "[Embedding]"
REMEDIATION_LOG_PREFIX = "[Remediation][Embedding]"

# advisory lock 的 bigint key（pg_advisory_lock 单参数版）
_REINDEX_LOCK_KEY = 742_301_990_122  # hashtext('skillhub:embedding_reindex') 量级的固定值

# 批量编码大小：批越大单次请求内存峰值越高（批内全部文本的向量同时在内存中），
# 且服务端推理耗时随条数线性增长（实测 ~25s 固定开销 + ~7s/条，CPU 受限环境）。
# 8 条单批约 80s，配合客户端 200s 超时留足余量，避免整批被超时放弃。
BATCH_SIZE = 8


@dataclass
class ReindexStats:
    """一次全量/增量任务的统计。"""

    total: int = 0           # 扫描到的目标行数
    indexed: int = 0         # 成功写入 embedding 的行数
    failed_batches: int = 0  # embedding 请求或写库失败的批次数
    failed_rows: int = 0     # embedding 请求或响应校验失败的行数
    skipped_empty: int = 0   # 文本为空被跳过的行数
    stale_skipped: int = 0   # 编码期间内容发生变化或记录消失的行数

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "indexed": self.indexed,
            "failed_batches": self.failed_batches,
            "failed_rows": self.failed_rows,
            "skipped_empty": self.skipped_empty,
            "stale_skipped": self.stale_skipped,
        }


class LockBusyError(RuntimeError):
    """另一个 embedding 索引任务正持有锁。"""


class SkillNotFoundError(LookupError):
    """指定的 Skill 不存在。"""


@dataclass(frozen=True)
class EmbeddingTarget:
    """一次读取快照中的待编码记录。"""

    skill_id: str
    text: str
    updated_at: datetime


def _validate_batch_size(batch_size: int) -> None:
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")


def _validate_embeddings(
    embeddings: list[list[float]], expected_count: int, expected_dimension: int
) -> None:
    if len(embeddings) != expected_count:
        raise ValueError(
            f"embedding response count mismatch: expected {expected_count}, "
            f"got {len(embeddings)}"
        )
    for index, embedding in enumerate(embeddings):
        if len(embedding) != expected_dimension:
            raise ValueError(
                f"embedding dimension mismatch at index {index}: "
                f"expected {expected_dimension}, got {len(embedding)}"
            )
        if not all(
            isinstance(value, (int, float)) and math.isfinite(value)
            for value in embedding
        ):
            raise ValueError(f"embedding contains non-finite value at index {index}")


async def _fetch_batch(
    after_skill_id: str | None, limit: int, only_missing: bool
) -> list[EmbeddingTarget]:
    """使用 keyset pagination 短会话读取一批待编码行。

    only_missing=True 时仅取 embedding IS NULL 且 risk_score IS NOT NULL 的行
    （与 _vector_search 的可见口径一致：无风险分的行对语义搜索不可见，
    无需消耗 embedding 计算）。
    """
    target_cond = (
        "embedding IS NULL AND risk_score IS NOT NULL"
        if only_missing
        else "TRUE"
    )
    cursor_cond = "AND skill_id > :after_skill_id" if after_skill_id is not None else ""
    sql = text(
        f"""
        SELECT skill_id, name, description, content, updated_at
        FROM skills
        WHERE {target_cond}
        {cursor_cond}
        ORDER BY skill_id
        LIMIT :limit
        """
    )
    params: dict[str, Any] = {"limit": limit}
    if after_skill_id is not None:
        params["after_skill_id"] = after_skill_id
    async with AsyncSessionLocal() as session:
        result = await session.execute(sql, params)
        rows = result.all()
    return [
        EmbeddingTarget(
            skill_id=row[0],
            text=prepare_skill_text(
                {"name": row[1], "description": row[2], "content": row[3]}
            ),
            updated_at=row[4],
        )
        for row in rows
    ]


async def _count_targets(only_missing: bool) -> int:
    cond = (
        "embedding IS NULL AND risk_score IS NOT NULL" if only_missing else "TRUE"
    )
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(f"SELECT count(*) FROM skills WHERE {cond}"))
        return result.scalar() or 0


async def _fetch_target(skill_id: str) -> EmbeddingTarget | None:
    """读取单个 Skill 的文本快照。"""
    sql = text(
        """
        SELECT skill_id, name, description, content, updated_at
        FROM skills
        WHERE skill_id = :skill_id
        """
    )
    async with AsyncSessionLocal() as session:
        result = await session.execute(sql, {"skill_id": skill_id})
        row = result.first()
    if row is None:
        return None
    return EmbeddingTarget(
        skill_id=row[0],
        text=prepare_skill_text(
            {"name": row[1], "description": row[2], "content": row[3]}
        ),
        updated_at=row[4],
    )


async def _write_embeddings(
    pairs: list[tuple[EmbeddingTarget, list[float]]],
) -> int:
    """仅在内容版本未变化时批量写库，返回更新行数。"""
    from src.models.repository import SkillRepository

    ordered = [
        (target.skill_id, target.updated_at, embedding)
        for target, embedding in sorted(pairs, key=lambda pair: pair[0].skill_id)
    ]
    async with AsyncSessionLocal() as session:
        repo = SkillRepository(session)
        return await repo.update_embeddings_batch(ordered)


async def _encode_and_store(
    targets: list[EmbeddingTarget], stats: ReindexStats, log_prefix: str = LOG_PREFIX
) -> None:
    """编码并写库一个批次；批处理失败按二分递归重试，隔离坏行。

    需要拆半的失败（超时 / 响应形状异常）只在批量体量大时发生：
    整批文本合计超过 embedding 服务客户端超时（200s），或个别坏行导致
    响应校验失败污染整批。此时若整批跳过，下一轮增量按相同排序会凑出
    同样的批次、再次失败，形成永久缺口。二分后子批体量减半（可到单行），
    保证本轮推进，坏行被隔离成单行失败。

    targets 按 skill_id 升序（_fetch_batch 的 keyset 排序），二分切割后
    子批保持有序，与 update_embeddings_batch 的排序写口径一致。
    """
    try:
        embeddings = await generate_embeddings([target.text for target in targets])
        _validate_embeddings(
            embeddings,
            expected_count=len(targets),
            expected_dimension=get_settings().ai.embedding_dimension,
        )
        pairs = [
            (target, embedding)
            for target, embedding in zip(targets, embeddings, strict=True)
        ]
        updated = await _write_embeddings(pairs)
        stats.indexed += updated
        stats.stale_skipped += len(pairs) - updated
    except Exception as exc:
        if len(targets) == 1 or not _is_retriable_encode_failure(exc):
            stats.failed_batches += 1
            stats.failed_rows += len(targets)
            logger.exception(
                "%s batch failed, skipped (skill_id=%s, size=%d, chars=%d, error=%s)",
                log_prefix,
                targets[0].skill_id,
                len(targets),
                _total_chars(targets),
                _format_encode_error(exc),
            )
            return
        mid = len(targets) // 2
        logger.warning(
            "%s batch failed, bisect retry (skill_id=%s, size=%d, chars=%d, error=%s, halve=%d/%d)",
            log_prefix,
            targets[0].skill_id,
            len(targets),
            _total_chars(targets),
            _format_encode_error(exc),
            mid,
            len(targets) - mid,
        )
        await _encode_and_store(targets[:mid], stats, log_prefix)
        await _encode_and_store(targets[mid:], stats, log_prefix)


def _total_chars(targets: list[EmbeddingTarget]) -> int:
    """批次文本总字符量，用于日志定位体量相关问题。"""
    return sum(len(target.text) for target in targets)


def _format_encode_error(exc: Exception) -> str:
    """把异常转为可读文本：类型名 + 消息。

    httpx.TimeoutException 等异常的 str() 为空串，直接 %s 打印只剩一个逗号，
    无法判断失败类型；统一为 "TypeName: message" 形式，消息为空时仅保留类型。
    """
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _is_retriable_encode_failure(exc: Exception) -> bool:
    """判定编码阶段可拆半重试的失败（值得缩小批次体量再试）。

    - httpx.TimeoutException（ReadTimeout/WriteTimeout/ConnectTimeout 等）：
      文本量越大越容易触发，拆半可解；
    - ValueError：响应校验失败（数量/维度/非有限值），多由个别坏行引发，
      拆半隔离出坏行后其余行可正常入库。

    服务不可达（ConnectError）、HTTPStatusError、写库失败等与输入体量无关，
    拆半只会放大失败面，保持整批跳过、由下轮增量自愈。
    """
    if isinstance(exc, httpx.TimeoutException):
        return True
    return isinstance(exc, ValueError)


async def _run_batches(
    only_missing: bool,
    stats: ReindexStats,
    batch_size: int,
    progress_cb=None,
    log_prefix: str = LOG_PREFIX,
) -> None:
    """批次主循环：读 → 编码 → 写，每批独立短会话。单批失败跳过继续。"""
    stats.total = await _count_targets(only_missing)
    after_skill_id: str | None = None
    while True:
        rows = await _fetch_batch(after_skill_id, batch_size, only_missing)
        if not rows:
            return
        # 无论空文本或批次失败都推进游标，避免增量模式反复读取同一批。
        after_skill_id = rows[-1].skill_id
        logger.info(
            "%s chunk: rows=%d cursor=%s", log_prefix, len(rows), after_skill_id,
        )

        to_encode = [target for target in rows if target.text.strip()]
        stats.skipped_empty += len(rows) - len(to_encode)
        if to_encode:
            await _encode_and_store(to_encode, stats, log_prefix)

        if progress_cb:
            progress_cb(stats)

        if len(rows) < batch_size:
            return


async def embed_missing(batch_size: int = BATCH_SIZE) -> ReindexStats:
    """增量补嵌入：仅处理 embedding IS NULL 且 risk_score IS NOT NULL 的行。

    锁被全量任务持有时抛 LockBusyError，由调用方决定跳过。
    """
    _validate_batch_size(batch_size)
    logger.info("%s backfill started", REMEDIATION_LOG_PREFIX)
    async with AsyncSessionLocal() as lock_session:
        locked = await lock_session.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _REINDEX_LOCK_KEY}
        )
        if not locked.scalar():
            raise LockBusyError("embedding reindex lock is held by another task")
        try:
            stats = ReindexStats()
            await _run_batches(
                only_missing=True, stats=stats, batch_size=batch_size,
                log_prefix=REMEDIATION_LOG_PREFIX,
            )
            logger.info(
                "%s backfill finished: total=%d indexed=%d failed_batches=%d "
                "failed_rows=%d skipped_empty=%d stale_skipped=%d",
                REMEDIATION_LOG_PREFIX,
                stats.total,
                stats.indexed,
                stats.failed_batches,
                stats.failed_rows,
                stats.skipped_empty,
                stats.stale_skipped,
            )
            return stats
        finally:
            await lock_session.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _REINDEX_LOCK_KEY}
            )


async def reindex_skill(skill_id: str) -> ReindexStats:
    """重建单个 Skill 的 embedding，与全量及增量任务互斥。"""
    async with AsyncSessionLocal() as lock_session:
        locked = await lock_session.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _REINDEX_LOCK_KEY}
        )
        if not locked.scalar():
            raise LockBusyError("embedding reindex lock is held by another task")
        try:
            target = await _fetch_target(skill_id)
            if target is None:
                raise SkillNotFoundError(skill_id)

            stats = ReindexStats(total=1)
            if not target.text.strip():
                stats.skipped_empty = 1
                return stats

            try:
                embeddings = await generate_embeddings([target.text])
                _validate_embeddings(
                    embeddings,
                    expected_count=1,
                    expected_dimension=get_settings().ai.embedding_dimension,
                )
                stats.indexed = await _write_embeddings([(target, embeddings[0])])
                stats.stale_skipped = 1 - stats.indexed
            except Exception:
                stats.failed_batches = 1
                stats.failed_rows = 1
                logger.exception(
                    "%s single skill reindex failed (skill_id=%s)", LOG_PREFIX, skill_id
                )
            return stats
        finally:
            await lock_session.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _REINDEX_LOCK_KEY}
            )


async def reindex_all(batch_size: int = BATCH_SIZE, progress_cb=None) -> ReindexStats:
    """全量重建：重算所有 skill 的 embedding，结束后追加 NULL 尾扫兜底
    mid-run 新插入的行。锁被占用时抛 LockBusyError。
    """
    _validate_batch_size(batch_size)
    async with AsyncSessionLocal() as lock_session:
        locked = await lock_session.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _REINDEX_LOCK_KEY}
        )
        if not locked.scalar():
            raise LockBusyError("embedding reindex lock is held by another task")
        try:
            stats = ReindexStats()
            await _run_batches(
                only_missing=False,
                stats=stats,
                batch_size=batch_size,
                progress_cb=progress_cb,
            )
            # 尾扫：兜住全量期间 discover 新插入（embedding IS NULL）的行
            tail = ReindexStats()
            await _run_batches(
                only_missing=True, stats=tail, batch_size=batch_size
            )
            stats.total += tail.total
            stats.indexed += tail.indexed
            stats.failed_batches += tail.failed_batches
            stats.failed_rows += tail.failed_rows
            stats.skipped_empty += tail.skipped_empty
            stats.stale_skipped += tail.stale_skipped
            return stats
        finally:
            await lock_session.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _REINDEX_LOCK_KEY}
            )
