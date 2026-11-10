"""独立后台 worker 进程。

把原先挂在 API lifespan 里的后台引导链整体迁到本进程（单副本 Deployment），
让 API 变成无状态、可多副本的纯 Web 服务：

- 一次性迁移 (run_migration)
- 启动补缺口 (run_remediation_on_startup)
- discover 定时调度器（含首轮扫描，audit 会等待其首轮完成）
- LLM 审核调度器（等待 discover 首轮完成后启动）
- Skillspector 采集器（security.enable_audit 时）

启用开关与异常处理与原本挂在 API lifespan 时保持一致：单任务异常只记录日志
不中断后续步骤，CancelledError 直接向上抛出以便干净停机。

入口：

    python -m src.jobs.worker
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

import structlog

from src.ai.embedding import close_embedding_service
from src.ai.translation import close_translation_service
from src.core.config import get_settings
from src.jobs.audit_llm_review import start_audit_llm_review_scheduler
from src.jobs.discover_scheduler import start_discover_scheduler
from src.jobs.migration import run_migration
from src.jobs.remediation import run_remediation_on_startup
from src.security.detector import start_skillspector_collector

settings = get_settings()
logger = structlog.get_logger()


def configure_worker_logging() -> None:
    """与 api.main.configure_logging 同一套口径，但日志落在 worker.log。

    worker 与 api 现在是两个独立进程，各自持有独立的 httpx 客户端与文件句柄，
    因此不能共用 api.log（否则轮转时会互相覆盖）。
    """
    level_name = settings.logging.level.strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    if not root_logger.handlers:
        root_handler = logging.StreamHandler(sys.stdout)
        root_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root_logger.addHandler(root_handler)

    if not any(isinstance(h, logging.FileHandler) for h in root_logger.handlers):
        log_dir = Path(settings.storage.local_path).expanduser().resolve() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "worker.log"
        max_bytes = max(0, int(settings.logging.max_bytes))
        backup_count = max(0, int(settings.logging.backup_count))
        if max_bytes > 0:
            file_handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
        else:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        root_logger.addHandler(file_handler)

    for handler in root_logger.handlers:
        handler.setLevel(level)

    # 压制 SQLAlchemy 池连接/归还及引擎的 DEBUG 日志，与 api 口径一致。
    for noisy_parent in ("sqlalchemy.pool", "sqlalchemy.engine", "sqlalchemy.orm"):
        logging.getLogger(noisy_parent).setLevel(logging.WARNING)
        for name, registered in logging.Logger.manager.loggerDict.items():
            if name.startswith(noisy_parent + ".") and isinstance(registered, logging.Logger):
                registered.setLevel(logging.NOTSET)

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        context_class=dict,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


async def _run_background_tasks():
    """按原 lifespan 的顺序与启用开关启动全部后台任务。

    返回 (collector, discover_scheduler, audit_scheduler) 引用，供 shutdown 阶段
    按逆序停止；未启动的返回 None。
    """
    collector = None
    if settings.security.enable_audit:
        logger.info("starting skillspector collector")
        try:
            collector = await start_skillspector_collector()
            logger.info("skillspector collector started")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("skillspector collector start failed")
    else:
        logger.info("security.enable_audit is false, skipping skillspector collector")

    discover_scheduler = None
    audit_scheduler = None

    # 一次性幂等迁移先于 discover 扫描执行；失败仅记录，不阻断后续步骤。
    logger.info("running startup migration")
    try:
        await run_migration()
        logger.info("startup migration completed")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("startup migration failed")

    # 启动补一轮衍生字段缺口（security/category/embedding）并落盘。
    logger.info("running startup remediation")
    try:
        await run_remediation_on_startup()
        logger.info("startup remediation completed")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("startup remediation failed")

    if settings.discover_scheduler.enabled:
        logger.info("starting discover scheduler")
        try:
            discover_scheduler = await start_discover_scheduler()
            logger.info("discover scheduler started")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("discover scheduler start failed")
    else:
        logger.info("discover_scheduler disabled, skipping discover")

    # LLM 审核调度器：依赖 migration 与 discover 首轮扫描完成后启动，
    # 避免审核与首轮扫描并发争抢资源。
    if settings.audit_llm_review.enabled:
        logger.info("starting audit llm review scheduler (waiting for discover first round)")
        try:
            if discover_scheduler is not None:
                await discover_scheduler.first_round_done.wait()
            audit_scheduler = await start_audit_llm_review_scheduler()
            logger.info("audit llm review scheduler started")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("audit llm review scheduler start failed")
    else:
        logger.info("audit_llm_review disabled, skipping audit llm review scheduler")

    return collector, discover_scheduler, audit_scheduler


async def _worker_main() -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            # Windows 等无 add_signal_handler 的环境不阻断启动。
            pass

    logger.info("skillhub worker starting up")
    collector, discover_scheduler, audit_scheduler = await _run_background_tasks()
    logger.info("skillhub worker background tasks started")

    try:
        await stop_event.wait()
    finally:
        # 逆序停止：audit → discover → collector → 关闭进程级 httpx 客户端。
        if audit_scheduler is not None:
            await audit_scheduler.stop()
        if discover_scheduler is not None:
            await discover_scheduler.stop()
        if collector is not None:
            await collector.stop()
        await close_embedding_service()
        await close_translation_service()
        logger.info("skillhub worker shutting down")


def main() -> int:
    configure_worker_logging()
    try:
        asyncio.run(_worker_main())
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())