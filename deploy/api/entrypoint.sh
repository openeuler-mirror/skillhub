#!/bin/sh
set -eu

python - <<'PY'
import asyncio
import subprocess

import asyncpg

from src.core.config import get_settings

# 应用内固定的迁迁移锁 key：api 多副本 + worker 都跑 alembic upgrade head，
# 用 PostgreSQL 会话级 advisory lock 串行化，避免并发建表互相冲突。
MIGRATION_LOCK_KEY = 725000001


async def main() -> None:
    postgres = get_settings().postgres

    # 先等数据库就绪（与后端连接的同一套参数）。
    for attempt in range(1, 31):
        try:
            conn = await asyncpg.connect(
                host=postgres.host,
                port=postgres.port,
                user=postgres.user,
                password=postgres.password,
                database=postgres.db,
                timeout=2,
            )
            await conn.close()
            print("Database is ready", flush=True)
            break
        except Exception as exc:
            if attempt == 30:
                raise RuntimeError("Database did not become ready within 60 seconds") from exc
            print(f"Waiting for database ({attempt}/30): {exc}", flush=True)
            await asyncio.sleep(2)

    # 持有会话锁跨 alembic 子进程；连接关闭时锁自动释放，无需显式 unlock。
    lock_conn = await asyncpg.connect(
        host=postgres.host,
        port=postgres.port,
        user=postgres.user,
        password=postgres.password,
        database=postgres.db,
    )
    try:
        await lock_conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
        print("Running database migrations", flush=True)
        subprocess.run(["alembic", "upgrade", "head"], check=True)
        print("Database migrations completed", flush=True)
    finally:
        await lock_conn.close()


asyncio.run(main())
PY

exec "$@"