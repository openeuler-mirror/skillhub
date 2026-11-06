"""启动后台补分类：为 category 为空的 skills 行补跑 DeepSeek 分类。

自愈缺口：分类降级落空（LLM 超时/503）的行，若 skill 内容一直不变，
reconcile 的 `_is_content_unchanged` 会跳过更新，空分类永远补不上；
本任务独立于扫描，在 API 启动后后台补跑一次（与 ``embed_missing`` 的
启动自愈模式一致），每轮只处理 category IS NULL 的行，跑完即止。

并发安全：
- pg_advisory_lock 与 discover 的分类/落库互斥，锁被占用时跳过本轮；
- 写库用 ``WHERE id=:id AND category IS NULL`` 条件更新，永不覆盖已分类值。

批次短事务（与 embedding_indexer 同款三段式，分类是 LLM HTTP 调用、
期间不占 DB 连接）：
  ① 短会话 keyset 读一批 (id, name, content, extra_metadata) → 关闭
  ② 无 DB 连接逐条调分类器（同步 LLM 调用，放线程池）
  ③ 短会话按 id 排序条件 UPDATE → commit → 关闭
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text

from src.ai.category_classifier import DeepSeekCategoryClassifier
from src.core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)
LOG_PREFIX = "[Remediation][Category]"

# advisory lock 的 bigint key（pg_advisory_lock 单参数版）
_CATEGORY_LOCK_KEY = 742_301_990_123

# 单批行数：批内逐条串行调 LLM，批大小只影响一次事务的写入行数
CHUNK_SIZE = 32


class LockBusyError(RuntimeError):
    """另一个分类任务（或 discover 扫描）正持有锁。"""


@dataclass
class BackfillStats:
    """一轮补分类任务的统计。"""

    total: int = 0          # 扫描到的空分类行数
    filled: int = 0         # 成功写入分类的行数
    unclassified: int = 0   # 分类器无结果（frontmatter 也无合法值）的行数
    failed: int = 0         # 分类调用抛错（含 fatal）的行数
    skipped: str | None = None  # 本轮跳过原因（锁被占用等）

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "filled": self.filled,
            "unclassified": self.unclassified,
            "failed": self.failed,
            "skipped": self.skipped,
        }


@dataclass(frozen=True)
class _UnclassifiedRow:
    """一批读取快照中的一行待分类记录。"""

    id: uuid.UUID
    name: str
    content: str | None
    extra_metadata: dict[str, Any] = field(default_factory=dict)


def _build_classifier():
    """初始化分类器；失败（如 catalog 同步失败）返回 None 由调用方跳过。"""
    return DeepSeekCategoryClassifier()


async def _fetch_chunk(after_id: uuid.UUID | None, limit: int) -> list[_UnclassifiedRow]:
    """keyset 分页读取一批空分类行（短会话）。"""
    cursor_cond = "AND id > :after_id" if after_id is not None else ""
    sql = text(
        f"""
        SELECT id, name, content, extra_metadata
        FROM skills
        WHERE category IS NULL OR category = ''
        {cursor_cond}
        ORDER BY id
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
        _UnclassifiedRow(
            id=row[0],
            name=row[1] or "",
            content=row[2],
            extra_metadata=dict(row[3] or {}),
        )
        for row in rows
    ]


async def _classify_rows(
    rows: list[_UnclassifiedRow], classifier: Any, stats: BackfillStats
) -> list[tuple[uuid.UUID, str]]:
    """逐条分类（同步 LLM 调用放线程池），返回可写库的 (id, category) 对。"""
    pairs: list[tuple[uuid.UUID, str]] = []
    for row in rows:
        try:
            category = await asyncio.to_thread(
                classifier.classify,
                skill_file=Path(f"{row.name or row.id}/SKILL.md"),
                metadata=row.extra_metadata,
                content=row.content or "",
                source_url=str(row.extra_metadata.get("source_url") or ""),
            )
        except Exception:
            stats.failed += 1
            logger.exception("%s classify exception: skill_id=%s", LOG_PREFIX, row.id)
            continue
        if category is None:
            stats.unclassified += 1
            logger.info(
                "%s unclassified: skill_id=%s (no valid frontmatter or LLM result)",
                LOG_PREFIX, row.id,
            )
            continue
        pairs.append((row.id, category))
    return pairs


async def _write_categories(pairs: list[tuple[uuid.UUID, str]]) -> int:
    """条件更新：只写仍为空的行，永不覆盖已有分类。按 id 排序写消除并发死锁。"""
    updated = 0
    async with AsyncSessionLocal() as session:
        for row_id, category in sorted(pairs, key=lambda pair: pair[0]):
            result = await session.execute(
                text(
                    "UPDATE skills SET category = :category "
                    "WHERE id = :id AND (category IS NULL OR category = '')"
                ),
                {"id": row_id, "category": category},
            )
            updated += result.rowcount or 0
        await session.commit()
    return updated


async def backfill_missing_categories(chunk_size: int = CHUNK_SIZE) -> BackfillStats:
    """补跑所有空分类行；锁被占用时抛 LockBusyError，由调用方决定跳过。"""
    stats = BackfillStats()
    logger.info("%s backfill started", LOG_PREFIX)
    async with AsyncSessionLocal() as lock_session:
        locked = await lock_session.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _CATEGORY_LOCK_KEY}
        )
        if not locked.scalar():
            raise LockBusyError("category backfill lock is held by another task")
        try:
            classifier = await asyncio.to_thread(_build_classifier)
            if classifier is None:
                stats.skipped = "category classifier unavailable"
                logger.warning("%s skipped: classifier unavailable", LOG_PREFIX)
                return stats

            after_id: uuid.UUID | None = None
            while True:
                rows = await _fetch_chunk(after_id, chunk_size)
                if not rows:
                    break
                # 无论分类结果如何都推进游标，避免反复读取同一批。
                after_id = rows[-1].id
                stats.total += len(rows)
                logger.info(
                    "%s chunk: gap_rows=%d cursor=%s", LOG_PREFIX, len(rows), after_id,
                )

                pairs = await _classify_rows(rows, classifier, stats)
                if pairs:
                    stats.filled += await _write_categories(pairs)

                if len(rows) < chunk_size:
                    break
            logger.info(
                "%s backfill finished: total=%d filled=%d unclassified=%d failed=%d",
                LOG_PREFIX, stats.total, stats.filled, stats.unclassified, stats.failed,
            )
            return stats
        finally:
            await lock_session.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": _CATEGORY_LOCK_KEY}
            )
