import logging
import logging.handlers
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from src.api.routes import agents, health, index, skills
from src.api.middleware import install_response_wrapper
from src.core.config import get_settings
from src.core.rate_limit import limiter

settings = get_settings()


class _HealthProbeFilter(logging.Filter):
    """过滤健康探针的访问日志（/api/v1/health 及裸 /health）。

    uvicorn access 记录的 msg 形如 'GET /api/v1/health HTTP/1.1'，
    探针每 10~30s 一次，全量保留会淹没有用信息。
    """

    _PROBE_PATHS = ("/health", "/api/v1/health")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # uvicorn.access 参数顺序: (client_addr, method, path, http_version, status)
            path = record.args[2].split("?")[0]
        except (AttributeError, IndexError, TypeError):
            return True
        return path not in self._PROBE_PATHS


def configure_logging() -> None:
    level_name = settings.logging.level.strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    # Use logging.level as the single switch for standard Python, Uvicorn,
    # application, and third-party loggers that already exist at startup.
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    if not root_logger.handlers:
        root_handler = logging.StreamHandler(sys.stdout)
        root_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s"
            )
        )
        root_logger.addHandler(root_handler)
    # 文件日志：与 skillcrawler/main.py 口径一致，落在
    # storage.local_path/logs/ 下，便于排查线上问题（stdout 会被容器回收）。
    # 按 logging.max_bytes/backup_count 轮转，避免长期运行把磁盘写满。
    if not any(isinstance(h, logging.FileHandler) for h in root_logger.handlers):
        log_dir = Path(settings.storage.local_path).expanduser().resolve() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "api.log"
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
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        root_logger.addHandler(file_handler)
    for handler in root_logger.handlers:
        handler.setLevel(level)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.setLevel(level)
        for handler in uvicorn_logger.handlers:
            handler.setLevel(level)

    # 健康探针（K8s liveness/readiness、compose healthcheck）每 10~30s 打一次
    # /api/v1/health，access 日志被刷屏淹没有用信息；探针请求不记访问日志，
    # 探针频率本身由部署侧 periodSeconds 控制。
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _HealthProbeFilter) for f in access_logger.filters):
        access_logger.addFilter(_HealthProbeFilter())

    for registered_logger in logging.Logger.manager.loggerDict.values():
        if not isinstance(registered_logger, logging.Logger):
            continue
        registered_logger.setLevel(level)
        for handler in registered_logger.handlers:
            handler.setLevel(level)

    # 压制 SQLAlchemy 池连接/归还及引擎的 DEBUG 日志
    for noisy_parent in ("sqlalchemy.pool", "sqlalchemy.engine", "sqlalchemy.orm"):
        logging.getLogger(noisy_parent).setLevel(logging.WARNING)
        # 重置子 logger 为 NOTSET，使其继承父级的 WARNING，不打印DEBUG信息
        for name, logger in logging.Logger.manager.loggerDict.items():
            if name.startswith(noisy_parent + ".") and isinstance(logger, logging.Logger):
                logger.setLevel(logging.NOTSET)

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger = structlog.get_logger()
    logger.info("skillhub starting up")

    collector = None
    if settings.security.enable_audit:
        from src.security.detector import start_skillspector_collector

        collector = await start_skillspector_collector()

    # 一次性迁移 + 启动补跑 + discover 调度器串成一条后台引导链：
    # migration → startup remediation → discover 首轮扫描，顺序保持不变，
    # 但整体放到后台，避免迁移（约 18s）阻塞 API 启动。
    # discover scheduler 的引用经 app.state 回传，供 shutdown 阶段停止。
    app.state.discover_scheduler = None
    app.state.audit_llm_review = None
    
    async def _bootstrap() -> None:
        # 一次性幂等迁移先于 discover 扫描执行；失败仅记录，不阻断后续步骤。
        try:
            from src.jobs.migration import run_migration
            await run_migration()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("startup migration failed")

        # 启动补一轮衍生字段缺口（security/category/embedding）并落盘；
        # 扫描后的缺口由 discover 每轮末尾的全表补齐兜底。
        try:
            from src.jobs.remediation import run_remediation_on_startup

            await run_remediation_on_startup()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("startup remediation failed")

        if settings.discover_scheduler.enabled:
            from src.jobs.discover_scheduler import start_discover_scheduler

            try:
                app.state.discover_scheduler = await start_discover_scheduler()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("discover scheduler start failed")
        
        # LLM 审核调度器：依赖 migration 与 discover 首轮扫描完成后启动。
        # 等待 discover 首轮结束信号，避免审核与首轮扫描并发争抢资源；
        # 引用经 app.state 回传，供 shutdown 阶段停止。
        if settings.audit_llm_review.enabled:
            from src.jobs.audit_llm_review import start_audit_llm_review_scheduler
    
            try:
                discover_scheduler = app.state.discover_scheduler
                if discover_scheduler is not None:
                    await discover_scheduler.first_round_done.wait()
                app.state.audit_llm_review = await start_audit_llm_review_scheduler()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("audit llm review scheduler start failed")               

    bootstrap_task = asyncio.create_task(_bootstrap(), name="startup-bootstrap")

    yield

    if not bootstrap_task.done():
        bootstrap_task.cancel()
    try:
        await bootstrap_task
    except asyncio.CancelledError:
        pass

    discover_scheduler = app.state.discover_scheduler
    if discover_scheduler is not None:
        await discover_scheduler.stop()
    audit_llm_review = app.state.audit_llm_review
    if audit_llm_review is not None:
        await audit_llm_review.stop()       
    if collector is not None:
        await collector.stop()
    from src.ai.embedding import close_embedding_service

    await close_embedding_service()
    from src.ai.translation import close_translation_service

    await close_translation_service()
    logger.info("skillhub shutting down")


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title="SkillHub API",
        description="Agent and Skill Discovery Platform",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.app.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # 下载接口用响应头回传制品摘要，浏览器端需要能读到
        expose_headers=["X-Artifact-SHA256", "Content-Disposition"],
    )
    install_response_wrapper(app)

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(skills.router, prefix="/api/v1/skills")
    app.include_router(agents.router, prefix="/api/v1/agents")
    app.include_router(index.router, prefix="/api/v1/index")

    return app


app = create_app()
