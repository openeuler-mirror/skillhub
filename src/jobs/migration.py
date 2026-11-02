"""一次性数据迁移：gitcode.com -> atomgit.com，CLI 脚本与 API 启动共用。

迁移内容（均幂等）：

1. 数据库字段（单事务）：文本列替换 ``gitcode.com`` 与 ``gitcode:``
   （skill_id / agent_id 前缀）两类子串；``source`` 列把 ``gitcode`` 归一为
   ``atomgit``。
2. 磁盘缓存：把 ``<storage.local_path>/skill-repositories`` 下的
   ``gitcode.com_*`` 目录改名为 ``atomgit.com_*``（目标已存在则跳过）。

触发点：

- CLI：``scripts/migrate_gitcode_to_atomgit.py``（手动执行）；
- API 启动：``src/api/main.py`` 的 lifespan 在启动 discover 调度器之前
  调用 ``run_gitcode_migration()``，迁移完成后再扫描。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from sqlalchemy import text

from src.core.config import get_settings
from src.core.database import sync_engine

logger = logging.getLogger(__name__)
LOG_PREFIX = "[Migration]"

# 文本列：替换 ``gitcode.com`` 与 ``gitcode:`` 两类子串
TEXT_COLUMNS: dict[str, list[str]] = {
    "skill_repos": ["repo_name", "url", "local_path"],
    "skills": ["skill_id", "source_url", "repo_url"],
    "skill_versions": ["skill_id", "source_url", "repo_url"],
    "agents": ["agent_id", "source_url"],
    "contributors": ["repo_url", "git_profile", "website"],
}

# JSONB 列：整体 cast 成 text 替换后写回
JSONB_COLUMNS: dict[str, list[str]] = {
    "skills": ["extra_metadata"],
    "skill_versions": ["extra_metadata"],
    "agents": ["extra_metadata"],
    "security_audits": ["details", "risk_signals"],
}

# source 列：直接归一 ``gitcode`` -> ``atomgit``
SOURCE_TABLES = ["skill_repos", "skills", "skill_versions", "agents", "contributors"]


def _count(conn, sql: str) -> int:
    return int(conn.execute(text(sql)).scalar() or 0)


def _plan_ops() -> list[tuple[str, str]]:
    """构造 (描述, update_sql) 列表：文本列 + source 列（便宜，每次启动都检查）。"""
    ops: list[tuple[str, str]] = []

    for table, columns in TEXT_COLUMNS.items():
        for column in columns:
            cond = f"{column} LIKE '%gitcode.com%' OR {column} LIKE '%gitcode:%'"
            expr = (
                f"replace(replace({column}, 'gitcode.com', 'atomgit.com'), "
                f"'gitcode:', 'atomgit:')"
            )
            ops.append((f"{table}.{column}", f"UPDATE {table} SET {column} = {expr} WHERE {cond}"))

    for table in SOURCE_TABLES:
        ops.append(
            (
                f"{table}.source",
                f"UPDATE {table} SET source = 'atomgit' WHERE source = 'gitcode'",
            )
        )

    return ops


def _plan_jsonb_ops() -> list[tuple[str, str]]:
    """构造 JSONB 列 (描述, update_sql)：整体 cast 成 text 替换后写回。

    这些列（尤其 security_audits.details / risk_signals 合起来 142MB）的
    ``(jsonb)::text LIKE '%gitcode.com%'`` 全表扫描无法走索引（jsonb 上的
    trigram/gin 索引对 LIKE 子串不生效），一次空跑就要十几秒；因此用完成
    标记保证只在首次迁移时扫描一次，之后跳过。
    """
    ops: list[tuple[str, str]] = []
    for table, columns in JSONB_COLUMNS.items():
        for column in columns:
            cond = f"({column})::text LIKE '%gitcode.com%' OR ({column})::text LIKE '%gitcode:%'"
            expr = (
                f"replace(replace({column}::text, 'gitcode.com', 'atomgit.com'), "
                f"'gitcode:', 'atomgit:')::jsonb"
            )
            ops.append((f"{table}.{column}", f"UPDATE {table} SET {column} = {expr} WHERE {cond}"))
    return ops


def _jsonb_done_marker() -> Path:
    """JSONB 列迁移完成标记路径（落在 storage.local_path 持久卷上）。"""
    return Path(get_settings().storage.local_path).expanduser() / ".gitcode_jsonb_migration_done"


def _apply_ops(conn, ops: list[tuple[str, str]], dry_run: bool) -> int:
    """对 (描述, update_sql) 逐条统计并迁移，返回受影响行数。"""
    total = 0
    for description, update_sql in ops:
        where_clause = update_sql.split(" WHERE ", 1)[1]
        table = description.split(".", 1)[0]
        count = _count(conn, f"SELECT count(*) FROM {table} WHERE {where_clause}")
        if count:
            total += count
            logger.info("%s %s: %d row(s)", LOG_PREFIX, description, count)
        if not dry_run and count:
            conn.execute(text(update_sql))
    return total


def migrate_database(dry_run: bool = False) -> int:
    """迁移数据库字段，返回受影响行数；dry_run 仅统计不写入。

    文本列与 source 列每次启动都检查（代价可忽略，且是源引用的主线列）；
    JSONB 列首次迁移成功（含空跑无残留）后落标记并跳过，避免对
    security_audits 等大表的 ``(jsonb)::text LIKE`` 全表扫描在每次空跑时
    重复十几秒。dry_run 不落标记。

    取舍：标记落盘后，后续新写入 JSONB 列（主要是 security_audits.details /
    risk_signals）中的 ``gitcode.com`` / ``gitcode:`` 残留不会再被捕获；源引用
    主线列（skill_repos.url / skills.source_url 等）不受影响、每次仍全量检查。
    如需恢复 JSONB 兜底，删除 ``<storage.local_path>/.gitcode_jsonb_migration_done``
    即可在下一次启动重新扫描并重新落标记。
    """
    total = 0
    marker = _jsonb_done_marker()
    jsonb_done = marker.exists()

    with sync_engine.begin() as conn:
        total += _apply_ops(conn, _plan_ops(), dry_run)
        if not jsonb_done:
            total += _apply_ops(conn, _plan_jsonb_ops(), dry_run)

    if not dry_run and not jsonb_done:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    return total


def rename_disk_caches(dry_run: bool = False) -> tuple[int, int]:
    """把 skill-repositories 下的 gitcode.com_* 目录改名为 atomgit.com_*。

    返回 (renamed, skipped)。目标目录已存在时跳过（并发实例或历史残留），
    rename 因目标被并发创建而失败同样算跳过。
    """
    local_path = Path(get_settings().storage.local_path).expanduser()
    repos_dir = local_path / "skill-repositories"
    if not repos_dir.is_dir():
        logger.info("%s 磁盘缓存目录不存在，跳过：%s", LOG_PREFIX, repos_dir)
        return 0, 0

    renamed = 0
    skipped = 0
    for old_dir in sorted(repos_dir.glob("gitcode.com_*")):
        if not old_dir.is_dir():
            continue
        new_dir = old_dir.with_name(old_dir.name.replace("gitcode.com_", "atomgit.com_", 1))
        if new_dir.exists():
            logger.info("%s 跳过（目标已存在）：%s", LOG_PREFIX, old_dir.name)
            skipped += 1
            continue
        if dry_run:
            logger.info("%s [dry-run] 将改名 %s -> %s", LOG_PREFIX, old_dir, new_dir.name)
            renamed += 1
            continue
        try:
            old_dir.rename(new_dir)
            renamed += 1
        except OSError:
            # 目标目录被并发实例创建（rename 到已存在目录在 Linux 上报 OSError）
            logger.info("%s 跳过（并发冲突）：%s", LOG_PREFIX, old_dir.name)
            skipped += 1

    logger.info("%s 磁盘缓存：改名 %d 个，跳过 %d 个", LOG_PREFIX, renamed, skipped)
    return renamed, skipped


def run_gitcode_migration(dry_run: bool = False) -> dict[str, int | float]:
    """
    一次性数据迁移（gitcode → atomgit）先于 discover 扫描执行；幂等，
    串行执行数据库迁移与磁盘改名，返回统计字典（含耗时）。
    已迁移过的再次执行为空跑。迁移完（含磁盘缓存改名）再启动调度器扫描。
    """
    started = time.monotonic()
    db_rows = migrate_database(dry_run=dry_run)
    renamed, skipped = rename_disk_caches(dry_run=dry_run)
    elapsed = time.monotonic() - started
    logger.info("%s 迁移完成：耗时 %.3fs", LOG_PREFIX, elapsed)
    return {
        "db_rows": db_rows,
        "disk_renamed": renamed,
        "disk_skipped": skipped,
        "elapsed_seconds": round(elapsed, 3),
    }


async def run_migration() -> None:
    """API 启动时执行迁移（幂等），阻塞操作放线程池，失败不影响启动。

    完成后由调用方（lifespan）紧接着启动 discover 调度器扫描。
    """
    try:
        summary = await asyncio.to_thread(run_gitcode_migration, dry_run=False)
        logger.info("%s 完成：%s", LOG_PREFIX, summary)
    except Exception:
        logger.exception("%s 失败，继续启动（扫描照常）", LOG_PREFIX)