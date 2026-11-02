import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

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
        return (
            f"postgresql+psycopg2://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"
        )


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
    gitcode_token: str = ""
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


class AuditLLMReviewConfig(BaseSettings):
    """后台定时 LLM 复核配置。

    每天在 ``start_time`` ~ ``end_time``（本地时区 HH:MM，容器 TZ 为
    Asia/Shanghai）窗口内，分批对已入库但尚未复核的 SkillSpector 风险项
    做误报判定；复核结果回写 security_audits 与 skills/skill_versions。
    """

    enabled: bool = False
    start_time: str = "01:00"
    end_time: str = "08:00"
    # OpenAI 兼容接口；默认硅基流动免费档 Qwen3.5-4B（与 skillspector 复核模型一致）
    model: str = "Qwen/Qwen3.5-4B"
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key: str = ""
    timeout: float = 90.0
    # 同一时刻最多并发的 LLM 请求数；硅基流动免费档实测可用并发约 3-4
    concurrency: int = 3
    # 单次请求最多送多少条命中；单轮 pass 单条审计最多复核多少条。
    # 一轮 pass 扫完后若窗口未结束且仍有剩余，会立即开始下一轮，
    # 因此队列空闲时单条审计当晚可连续推进多个批次（0 = 不限）。
    max_findings_per_call: int = 10
    max_findings_per_audit: int = 200
    # 复核上下文：本地 repo 缓存 -> 远端 raw -> 现有 snippet。
    # window 模式对小文件送全文，大文件按命中位置取窗口行。
    context_mode: Literal["snippet", "window", "full"] = "window"
    window_lines: int = 40
    window_header_lines: int = 40
    window_max_ranges: int = 24
    window_gap: int = 3
    full_file_max_bytes: int = 64 * 1024
    max_file_bytes: int = 512 * 1024
    max_context_chars: int = 60_000
    repo_cache_root: str = "/opt/wittyhub/skill-repositories"
    git_timeout: float = 20.0
    # 远端 raw 拉取（本地缓存缺失时）；失败/超时回退 snippet，不影响审计状态
    remote_fetch_enabled: bool = True
    remote_fetch_timeout: float = 15.0
    remote_fetch_max_bytes: int = 512 * 1024
    remote_fetch_max_files_per_audit: int = 40
    github_token: str = ""
    gitcode_token: str = ""
    gitlab_token: str = ""
    gitee_token: str = ""
    context_cache_max_bytes: int = 64 * 1024 * 1024
    max_output_tokens: int = 2048
    # 置信度门槛：模型确认命中但 confidence 低于该值时按上游口径丢弃；
    # 0 表示关闭，所有确认为真的命中都保留
    min_confidence: float = 0.6
    # 误报删除门槛：模型判定 is_vulnerability=false 但 confidence 低于该值时
    # 不删除、保留命中；HIGH/CRITICAL 使用更严格的 *_high 值。
    # 0 表示关闭该门槛（仅按模型结论删除）。
    min_false_positive_confidence: float = 0.85
    min_false_positive_confidence_high: float = 0.9
    # 单条审计复核失败的最大重试轮次（跨天累计），超过后不再自动重试
    max_attempts: int = 3
    # 客户端限流（与硅基流动免费档 1000 RPM / 80000 TPM 对齐；0 = 不限流）
    rpm: int = 1000
    tpm: int = 80000


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
    audit_llm_review: AuditLLMReviewConfig = Field(default_factory=AuditLLMReviewConfig)
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
            audit_llm_review=AuditLLMReviewConfig(**data.get("audit_llm_review", {})),
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


_HHMM_PATTERN = re.compile(r"^\d{1,2}:\d{2}$")


def _parse_env_value(value: str) -> Any:
    if value == "":
        return ""
    # 形如 "2:00" 的值在 YAML 1.1 里会被解析成六十进制整数（120），
    # 会让调度器的 HH:MM 解析失败；时间类配置一律保留字符串。
    if _HHMM_PATTERN.fullmatch(value.strip()):
        return value
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError:
        return value


@lru_cache
def get_settings() -> Settings:
    default_config_path = Path(__file__).resolve().parents[2] / "config.yaml"
    config_path = os.environ.get("WITTYHUB_CONFIG", str(default_config_path))
    return Settings.from_yaml(config_path)
