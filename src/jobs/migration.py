"""一次性数据迁移入口。

gitcode.com -> atomgit.com 的迁移（数据库字段替换 + 磁盘缓存改名）已完成，
当前无待执行的迁移任务，入口保留以维持启动流程的调用契约。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
LOG_PREFIX = "[Migration]"


async def run_migration() -> None:
    """API/worker 启动时调用的迁移入口。
    """
    logger.info("%s 当前无迁移任务）", LOG_PREFIX)