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
    # 大模型复核参数（skillspector 容器入口读取后注入为
    # SKILLSPECTOR_* / OPENAI_* 环境变量；后端仅解析 config.yaml，不消费这些字段）
    skillspector_llm_nodes: str = "meta_analyzer"
    skillspector_model: str = ""
    skillspector_base_url: str = ""
    skillspector_api_key: str = ""
    skillspector_text_only: bool = True
    skillspector_rule_scope: bool = True
    skillspector_window_lines: int = 20
    skillspector_window_header_lines: int = 40
    skillspector_rpm: int = 1000
    skillspector_tpm: int = 80000
    skillspector_max_output_tokens: int = 4096
    skillspector_disable_thinking: bool = True
    skillspector_log_level: str = "INFO"
    skillspector_max_concurrency: int = 4
    skillspector_batch_timeout: int = 300
    skillspector_llm_timeout: int = 90
    skillspector_llm_budget_seconds: int = 540


class AppConfig(BaseSettings):
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    admin_api_token: str = ""


class DiscoverSchedulerConfig(BaseSettings):
    """后台定时 discover 调度配置。

    每天在 ``start_time``（按 ``timezone`` 解释，默认北京时间）触发一轮，
    与容器 TZ 解耦。
    """

    enabled: bool = False
    # 触发时刻（HH:MM），按 timezone 解释
    start_time: str = "00:00"
    # 触发时刻时区；可用 DISCOVER_SCHEDULER__TIMEZONE 覆盖
    timezone: str = "Asia/Shanghai"
    result_dir: str = ""  # 每轮结果 JSON 保存目录；空则用 storage.local_path/logs


class AuditLLMReviewConfig(BaseSettings):
    """后台定时 LLM 复核配置。

    每天在 ``start_time`` ~ ``end_time``（按 ``timezone`` 解释，默认北京时间）
    窗口内，分批对已入库但尚未复核的 SkillSpector 风险项做误报判定；复核
    结果回写 security_audits 与 skills/skill_versions。
    """

    enabled: bool = False
    start_time: str = "01:00"
    end_time: str = "08:00"
    # 窗口时区：与容器 TZ 解耦，默认按北京时间解释 start_time/end_time；
    # 可用 AUDIT_LLM_REVIEW__TIMEZONE 覆盖
    timezone: str = "Asia/Shanghai"
    # OpenAI 兼容接口。model/base_url/api_key 留空时复用 model.providers
    # 当前 provider（默认 siliconflow / Qwen3.5-4B），避免与主模型配置重复；
    # 需要为复核单独指定模型时再用 AUDIT_LLM_REVIEW__* 覆盖。
    model: str = ""
    base_url: str = ""
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
    # 单次 LLM 调用耗时超过该值（秒）记为慢调用并打 warning；0 = 关闭
    slow_llm_seconds: float = 30.0
    # 客户端限流单次等待超过该值（秒）打 warning；0 = 关闭
    rate_limit_slow_wait_seconds: float = 30.0
    # 每轮结果 JSON 保存目录；留空则用 storage.local_path/logs（不跟随
    # discover_scheduler.result_dir，避免两类任务结果混在同一目录）
    result_dir: str = ""
    # 结果文件保留策略：按天数和份数双重限制，0 表示不限制该维度
    result_keep_days: int = 30
    result_max_files: int = 200


class LicenseGateConfig(BaseModel):
    """license 判定参数与准入清单。

    判定引擎与 openeuler-jenkins 门禁同源，但本平台只做三态标记、不删除。
    准入清单由 compliance SIG 维护，列出每个 license 的 oeApproved /
    lowRisk / black 属性；清单未收录的 license 默认判为 unknown（写 0）。
    """

    # 清单远端地址（AtomGit raw，无需鉴权）；本地缓存过期时回源拉取
    access_list_url: str = (
        "https://api.atomgit.com/api/v5/repos/openeuler/compliance/raw/"
        "doc/rule/Licenses_Access_List.yaml?ref=master"
    )
    # 清单本地缓存路径；空则使用 <storage.local_path>/license/Licenses_Access_List.yaml
    access_list_cache: str = ""
    # 缓存有效期（小时）；过期后优先回源，失败时降级使用过期缓存
    access_list_ttl_hours: int = 24
    # 远端拉取超时（秒）
    access_list_timeout: float = 30.0
    # 准入清单未收录、但允许分发的许可（不覆盖 black 判定）
    extra_allow: list[str] = Field(
        default_factory=lambda: [
            "CANN-2.0",
            "SUL-1.0",
            "FSL-1.1-Apache-2.0",
            "Convex-CPL",
            "CC-BY-3.0-US",
        ]
    )
    # 仅当 SKILL.md 声明无法解析（描述性占位）时是否回退到 LICENSE 文件；
    # 可解析的声明始终优先（声明优先原则，见 gate_engine.scan）。
    prefer_file: bool = True


class LicenseRepoConfig(BaseModel):
    """仓库文件树来源与远端拉取参数。

    优先级：``skill_repos.local_path`` 的本地 clone -> 远程缓存目录 ->
    按需浅克隆。discover 会为配置的仓库维护本地 clone，正常情况下从本地
    git 对象库物化 license 文件，不会触发完整远端 clone；本地文件树不可用
    时才使用这里的缓存/拉取配置。
    """

    # 远程缓存根目录；空则使用 <storage.local_path>/license-repos
    remote_cache_root: str = ""
    # 远程缓存有效期（天）；过期后重新拉取
    remote_cache_ttl_days: int = 7
    # 缓存保留期（天）：删除本轮未使用且超过该天数的远程/_local 缓存；0 表示禁用
    remote_cache_keep_days: int = 7
    # 本地 clone 缺失时是否允许远端浅克隆（关闭后只使用本地/已有缓存）
    remote_fetch: bool = True
    # 同时判定多少个仓库（每个仓库内部串行；discover 的仓库扫描另有限制）
    remote_concurrency: int = 4
    # 单次 git 命令超时（秒）；超时按仓库不可用处理（该行保持原值）
    git_timeout: float = 60.0


class LicenseCheckConfig(BaseModel):
    """license 三态标记任务配置。

    任务每天 ``start_time``（按 ``timezone`` 解释）触发一轮**全量**复核：
    判定 skills / skill_versions 的 license 合规性，写回 ``license_compliant``
    （1/0；失败保持原值）。判定与写值全部由该任务负责，discover 不参与。

    调度器启动后（worker 已完成 discover 首轮）会先立即执行一轮，之后按
    ``start_time`` 每日触发；同一时刻通过 PostgreSQL advisory lock 保证
    多副本下只有一个实例执行。
    """

    # 是否启用定时任务；关闭后 license_compliant 不再更新（已有值保留）
    enabled: bool = True
    # 每日触发时刻（HH:MM），按 timezone 解释
    start_time: str = "02:00"
    # 触发时刻时区；与容器 TZ 解耦，默认北京时间
    timezone: str = "Asia/Shanghai"
    # 每批提交多少行；控制单次事务大小与锁持有时间
    batch_size: int = 200
    gate: LicenseGateConfig = Field(default_factory=LicenseGateConfig)
    repo: LicenseRepoConfig = Field(default_factory=LicenseRepoConfig)


class LoggingConfig(BaseSettings):
    level: str = "INFO"
    format: str = "json"
    # 日志时间展示时区；未配置时默认北京时间
    timezone: str = "Asia/Shanghai"
    # api.log 轮转：单文件上限（字节）与保留份数；max_bytes=0 表示不轮转
    max_bytes: int = 100 * 1024 * 1024
    backup_count: int = 5


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
    license_check: LicenseCheckConfig = Field(default_factory=LicenseCheckConfig)
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
            license_check=LicenseCheckConfig(**data.get("license_check", {})),
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
