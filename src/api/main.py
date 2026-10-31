import asyncio
import logging
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
    if not any(isinstance(h, logging.FileHandler) for h in root_logger.handlers):
        log_dir = Path(settings.storage.local_path).expanduser().resolve() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "api.log", encoding="utf-8")
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


async def _run_remediation_loop() -> None:
    """启动后立即补一轮衍生字段缺口，之后低频定时补跑，失败不影响服务。

    覆盖三类缺口（security 风险分 / category 分类 / embedding 向量），
    详见 src/jobs/remediation.py：启动即补可自愈重启期间错过的补跑
    （discover 调度会跳过当天已过触发点），之后每小时一轮兜住长驻实例
    期间新增的缺口。单轮内部各任务自持 pg_advisory_lock 互斥。
    """
    from src.jobs.remediation import run_remediation_loop

    try:
        await run_remediation_loop()
    except asyncio.CancelledError:
        raise
    except Exception:
        structlog.get_logger().exception("Remediation loop failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger = structlog.get_logger()
    logger.info("wittyhub starting up")

    collector = None
    if settings.security.enable_audit:
        from src.security.detector import start_skillspector_collector

        collector = await start_skillspector_collector()

    # 一次性迁移先于 discover 扫描执行；幂等，
    from src.jobs.migration import run_migration

    await run_migration()

    discover_scheduler = None
    if settings.discover_scheduler.enabled:
        from src.jobs.discover_scheduler import start_discover_scheduler

        discover_scheduler = await start_discover_scheduler()

    # 启动时补跑衍生字段缺口（security/category/embedding），之后每小时一轮：
    remediation_task = asyncio.create_task(_run_remediation_loop())

    yield

    remediation_task.cancel()
    if discover_scheduler is not None:
        await discover_scheduler.stop()
    if collector is not None:
        await collector.stop()
    from src.ai.embedding import close_embedding_service

    await close_embedding_service()
    from src.ai.translation import close_translation_service

    await close_translation_service()
    logger.info("wittyhub shutting down")


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title="WittyHub API",
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
    )
    install_response_wrapper(app)

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(skills.router, prefix="/api/v1/skills")
    app.include_router(agents.router, prefix="/api/v1/agents")
    app.include_router(index.router, prefix="/api/v1/index")

    return app


app = create_app()
