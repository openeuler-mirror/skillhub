import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class PostgresConfig(BaseSettings):
    host: str = "localhost"
    port: int = 5432
    user: str = ""
    password: str = ""
    db: str = ""
    sslmode: str = "disable"

    @property
    def url(self) -> str:
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"

    @property
    def sync_url(self) -> str:
        # Keep the synchronous driver explicit. SQLAlchemy's default PostgreSQL
        # driver can vary across major releases; this project installs psycopg2.
        return f"postgresql+psycopg2://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"


class StorageConfig(BaseSettings):
    type: str = "local"
    local_path: str = "/opt/wittyhub"
    github_token: str = ""


class ModelProviderConfig(BaseModel):
    """单个 LLM provider 的连接配置（OpenAI 兼容 /chat/completions 接口）。"""

    name: str = ""
    base_url: str = ""
    api_key: str = ""
    timeout: float = 60


class ModelConfig(BaseSettings):
    # 平铺字段即"当前生效"的模型配置；配置了 provider 时由 resolve()
    # 用 providers 中对应条目填充。
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    timeout: float = 60
    # 当前选用的 provider（providers 的 key）；为空则直接使用上方平铺字段
    provider: str = ""
    providers: dict[str, ModelProviderConfig] = Field(default_factory=dict)

    def resolve(self) -> "ModelConfig":
        """返回当前生效 provider 的模型配置。

        provider 为空时原样返回（兼容只写平铺字段的旧配置）；provider
        指向 providers 中不存在的 key 时直接报错，避免静默回退到错误的
        模型/凭据。
        """
        if not self.provider:
            return self
        entry = self.providers.get(self.provider)
        if entry is None:
            available = ", ".join(sorted(self.providers)) or "<none>"
            raise ValueError(
                f"unknown model.provider {self.provider!r}, available: {available}"
            )
        return self.model_copy(
            update={
                "name": entry.name,
                "base_url": entry.base_url,
                "api_key": entry.api_key,
                "timeout": entry.timeout,
            }
        )


class CrawlerConfig(BaseSettings):
    github_token: str = ""
    atomgit_token: str = ""
    github_username: str = "git"
    max_tags_per_repo: int = 5


class SkillRepoEntry(BaseSettings):
    url: str
    branch: str | None = None


class SecurityConfig(BaseSettings):
    # Skillspector (Jenkins-based scanner)
    enable_audit: bool = False
    skillspector_jenkins_url: str = ""  # env: SECURITY__SKILLSPECTOR_JENKINS_URL
    skillspector_jenkins_user: str = ""  # env: SECURITY__SKILLSPECTOR_JENKINS_USER
    skillspector_jenkins_token: str = ""  # env: SECURITY__SKILLSPECTOR_JENKINS_TOKEN
    # 同步扫描等待 Jenkins 构建结束的超时（秒），默认 600（10 分钟）
    skillspector_timeout: float = 600.0  # env: SECURITY__SKILLSPECTOR_TIMEOUT
    # Jenkins 部署参数（skillspector 容器入口读取注入为 JENKINS_* 环境变量；
    # 后端仅解析 config.yaml，不消费这些字段）
    skillspector_jenkins_http_port: int = 8083
    skillspector_jenkins_num_executors: int = 10
    skillspector_jenkins_quiet_period: int = 5
    skillspector_repository_root: str = "/opt/wittyhub/skill-repositories"


class AppConfig(BaseSettings):
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    admin_api_token: str = ""


class DiscoverSchedulerConfig(BaseSettings):
    """后台定时 discover 调度配置。

    interval 支持 "daily"（每天）或 "weekly"（每周），触发时刻由 time
    （UTC HH:MM）决定；weekly 时 weekday 指定星期（mon/tue/wed/
    thu/fri/sat/sun）。
    """

    enabled: bool = False
    interval: str = "daily"
    time: str = "03:00"
    weekday: str = "sun"
    result_dir: str = ""  # 每轮结果 JSON 保存目录；空则用 storage.local_path/logs


class LoggingConfig(BaseSettings):
    level: str = "INFO"
    format: str = "json"


class AIConfig(BaseSettings):
    embedding_model: str = "BAAI/bge-base-zh-v1.5"
    embedding_host: str = "http://localhost:8082"
    embedding_dimension: int = 768
    enable_semantic_search: bool = True


class Settings(BaseSettings):
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    crawler: CrawlerConfig = Field(default_factory=CrawlerConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    app: AppConfig = Field(default_factory=AppConfig)
    discover_scheduler: DiscoverSchedulerConfig = Field(default_factory=DiscoverSchedulerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    openeuler_repos: list[SkillRepoEntry] = Field(default_factory=list)
    personal_repos: list[SkillRepoEntry] = Field(default_factory=list)
    enterprise_repos: list[SkillRepoEntry] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Settings":
        path = Path(path)
        data: dict[str, Any] = {}
        if path.exists():
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        _apply_env_overrides(data)

        return cls(
            postgres=PostgresConfig(**data.get("postgres", {})),
            storage=StorageConfig(**data.get("storage", {})),
            model=ModelConfig(**data.get("model", {})).resolve(),
            crawler=CrawlerConfig(**data.get("crawler", {})),
            security=SecurityConfig(**data.get("security", {})),
            app=AppConfig(**data.get("app", {})),
            discover_scheduler=DiscoverSchedulerConfig(**data.get("discover_scheduler", {})),
            logging=LoggingConfig(**data.get("logging", {})),
            ai=AIConfig(**data.get("ai", {})),
            openeuler_repos=data.get("openeuler_repos", []) or [],
            personal_repos=data.get("personal_repos", []) or [],
            enterprise_repos=data.get("enterprise_repos", []) or [],
        )


def _apply_env_overrides(data: dict[str, Any]) -> None:
    for key, value in os.environ.items():
        if "__" not in key:
            continue
        section, field = key.split("__", 1)
        section = section.strip().lower()
        field = field.strip().lower()
        if not section or not field:
            continue
        section_data = data.setdefault(section, {})
        if isinstance(section_data, dict):
            section_data[field] = _parse_env_value(value)


def _parse_env_value(value: str) -> Any:
    if value == "":
        return ""
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError:
        return value


@lru_cache
def get_settings() -> Settings:
    default_config_path = Path(__file__).resolve().parents[2] / "config.yaml"
    config_path = os.environ.get("WITTYHUB_CONFIG", str(default_config_path))
    return Settings.from_yaml(config_path)
