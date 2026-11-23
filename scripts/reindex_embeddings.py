#!/usr/bin/env python3
"""Embedding 重建脚本（独立进程，直接连库，无 HTTP 超时问题）。

用法:
    python scripts/reindex_embeddings.py [--batch-size 8] [--dry-run]
    python scripts/reindex_embeddings.py --skill-id <skill_id> [--dry-run]

行为:
    - 与后台增量任务通过 pg_advisory_lock 互斥；锁被占用时 fail-fast 退出
    - 重算全部 skill 的 embedding，结束后追加 NULL 尾扫兜底新插入行
    - 单批失败跳过继续，结束打印统计摘要

退出码: 0 正常完成；1 锁冲突或致命错误；3 Skill 不存在；4 重建不完整
"""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ai.embedding_indexer import (  # noqa: E402
    BATCH_SIZE,
    LockBusyError,
    ReindexStats,
    SkillNotFoundError,
    reindex_all,
    reindex_skill,
)
from src.core.config import get_settings  # noqa: E402


def batch_size(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 1000:
        raise argparse.ArgumentTypeError("batch size 必须在 1 到 1000 之间")
    return parsed


def print_progress(stats: ReindexStats) -> None:
    print(
        f"  processed={stats.indexed + stats.skipped_empty}/{stats.total}"
        f" indexed={stats.indexed} skipped_empty={stats.skipped_empty}"
        f" stale_skipped={stats.stale_skipped} failed_rows={stats.failed_rows}"
        f" failed_batches={stats.failed_batches}",
        flush=True,
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="全量或单 Skill embedding 重建")
    parser.add_argument(
        "--batch-size",
        type=batch_size,
        default=BATCH_SIZE,
        help="全量模式每批处理行数，范围 1-1000（默认 8）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只显示配置，不执行重建"
    )
    parser.add_argument(
        "--skill-id",
        help="只重建指定 skill_id；不指定时执行全量重建",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = get_settings()
    if not settings.ai.enable_semantic_search:
        print("ai.enable_semantic_search=false，语义搜索未启用，不执行重建")
        return 0

    print(f"embedding host: {settings.ai.embedding_host}")
    print(f"model: {settings.ai.embedding_model} (dim={settings.ai.embedding_dimension})")
    print(f"target: {args.skill_id or 'all skills'}")
    if args.dry_run:
        return 0

    start = time.monotonic()
    try:
        if args.skill_id:
            stats = await reindex_skill(args.skill_id)
        else:
            stats = await reindex_all(
                batch_size=args.batch_size, progress_cb=print_progress
            )
    except LockBusyError:
        print("ERROR: 另一个 embedding 索引任务正在运行（增量钩子或另一实例），稍后重试")
        return 1
    except SkillNotFoundError:
        print(f"ERROR: Skill 不存在：{args.skill_id}")
        return 3

    elapsed = time.monotonic() - start
    print(
        f"\n完成（{elapsed:.1f}s，target={args.skill_id or 'all'}）："
        f"目标 {stats.total} 行，写入 {stats.indexed}，"
        f"空文本跳过 {stats.skipped_empty}，并发变更跳过 {stats.stale_skipped}，"
        f"失败行 {stats.failed_rows}，失败批次 {stats.failed_batches}"
    )
    if stats.failed_batches or stats.stale_skipped:
        print("ERROR: 重建未完整覆盖全部目标行，请排查错误后重新执行")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
