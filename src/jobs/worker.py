"""独立后台 worker 进程。

把原先挂在 API lifespan 里的后台引导链整体迁到本进程（单副本 Deployment），
让 API 变成无状态、可多副本的纯 Web 服务：

- 一次性迁移 (run_migration)
- discover 定时调度器（含首轮扫描）
- license 复核调度器与 LLM 审核调度器：同属 discover 下游任务，首轮完成后
  并行启动；license 之后跟随每轮 discover 结束自动复核
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
from typing import Any

from src.ai.embedding import close_embedding_service
from src.ai.translation import close_translation_service
from src.core.config import get_settings
from src.core.logging import log_localtime
from src.jobs.audit_llm_review import start_audit_llm_review_scheduler
from src.jobs.discover_scheduler import start_discover_scheduler
from src.jobs.license_check import start_license_check_scheduler
from src.jobs.migration import run_migration
from src.security.detector import start_skillspector_collector

settings = get_settings()
logger = logging.getLogger(__name__)


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
        root_formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        root_formatter.converter = log_localtime
        root_handler.setFormatter(root_formatter)
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
        file_formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        file_formatter.converter = log_localtime
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)

    for handler in root_logger.handlers:
        handler.setLevel(level)

    # 压制 SQLAlchemy 池连接/归还及引擎的 DEBUG 日志，与 api 口径一致。
    for noisy_parent in ("sqlalchemy.pool", "sqlalchemy.engine", "sqlalchemy.orm"):
        logging.getLogger(noisy_parent).setLevel(logging.WARNING)
        for name, registered in logging.Logger.manager.loggerDict.items():
            if name.startswith(noisy_parent + ".") and isinstance(registered, logging.Logger):
                registered.setLevel(logging.NOTSET)


async def _run_background_tasks():
    """按原 lifespan 的顺序与启用开关启动全部后台任务。

    返回 (collector, discover_scheduler, license_scheduler, audit_scheduler)
    引用，供 shutdown 阶段按逆序停止；未启动的返回 None。
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
    license_scheduler = None

    # 一次性幂等迁移先于 discover 扫描执行；失败仅记录，不阻断后续步骤。
    logger.info("running startup migration")
    try:
        await run_migration()
        logger.info("startup migration completed")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("startup migration failed")

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

    # license 复核与 LLM 审核都是 discover 的下游任务：
    # - 首轮扫描结束后本地 clone 与入库数据才就绪，两者都应在此之后启动；
    # - 两者互不依赖，用 gather 并行启动，避免 license 首轮阻塞 LLM 审核
    #   （反之亦然）。license 之后会跟随每一轮 discover 结束自动触发；
    # - license 不删除任何 skill，只更新 license / license_compliant。
    # 下游任务表：(显示名, 零参工厂)。license 工厂用闭包捕获 discover_scheduler，
    # 以便后续跟随每一轮 discover 触发；audit 工厂不需要参数。
    downstream: list[tuple[str, Any]] = []
    if settings.license_check.enabled:
        downstream.append(
            (
                "license check",
                lambda: start_license_check_scheduler(discover_scheduler),
            )
        )
    else:
        logger.info("license_check disabled, skipping license check scheduler")
    if settings.audit_llm_review.enabled:
        downstream.append(
            ("audit llm review", start_audit_llm_review_scheduler)
        )
    else:
        logger.info("audit_llm_review disabled, skipping audit llm review scheduler")

    if downstream:
        if discover_scheduler is not None:
            logger.info(
                "starting downstream schedulers after discover first round: %s",
                ", ".join(name for name, _ in downstream),
            )
            await discover_scheduler.first_round_done.wait()
        # discover 未启用或启动失败时没有首轮信号可等，下游直接启动：
        # license 会退化为“启动即跑一轮 + 每日 start_time 触发”。
        # return_exceptions=True：单个调度器启动失败不影响另一个；CancelledError
        # 需要单独向上抛出，保证 worker 能干净停机。
        # gather 保持传入顺序，结果与 downstream 一一对应；按名称分派回
        # worker 持有的引用，供 shutdown 阶段逆序停止。
        results = await asyncio.gather(
            *(factory() for _, factory in downstream),
            return_exceptions=True,
        )
        for (name, _), result in zip(downstream, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                logger.error("%s scheduler start failed: %s", name, result)
                continue
            if name == "license check":
                license_scheduler = result
            else:
                audit_scheduler = result
            logger.info("%s scheduler started", name)

    return collector, discover_scheduler, license_scheduler, audit_scheduler


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
    collector, discover_scheduler, license_scheduler, audit_scheduler = await _run_background_tasks()
    logger.info("skillhub worker background tasks started")

    try:
        await stop_event.wait()
    finally:
        # 逆序停止：audit → license → discover → collector → 关闭进程级 httpx 客户端。
        if audit_scheduler is not None:
            await audit_scheduler.stop()
        if license_scheduler is not None:
            await license_scheduler.stop()
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
