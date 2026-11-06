from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class SkillBase(BaseModel):
    skill_id: str = Field(..., min_length=1, max_length=255)
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    version: str | None = Field(None, max_length=255)
    commit_id: str | None = Field(None, max_length=40)
    author: str | None = Field(None, max_length=255)
    source: str = Field(..., max_length=50)
    source_url: str = Field(..., min_length=1, max_length=2048)
    category: str | None = Field(None, max_length=100)
    tags: list[str] | None = Field(None, max_length=100)
    platform: str | None = Field(None, max_length=100)
    content: str | None = Field(None, max_length=2_000_000)
    extra_metadata: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("extra_metadata", "metadata"),
        serialization_alias="metadata",
    )

    @field_validator("source")
    @classmethod
    def validate_source(cls, v: str) -> str:
        if v == "gitcode":
            v = "atomgit"
        allowed = {"github", "atomgit", "gitlab", "gitee", "clawhub", "local"}
        if v not in allowed:
            raise ValueError(f"source must be one of {allowed}")
        return v


class SkillCreate(SkillBase):
    pass


class SkillUpdate(BaseModel):
    name: str | None = Field(None, max_length=255)
    description: str | None = None
    version: str | None = Field(None, max_length=255)
    category: str | None = Field(None, max_length=100)
    tags: list[str] | None = None
    extra_metadata: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("extra_metadata", "metadata"),
        serialization_alias="extra_metadata",
    )


class SkillResponse(SkillBase):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: str
    repo_url: str | None = None
    content: str | None = None
    category_label: str | None = None
    risk_score: int | None = None
    download_count: int = 0
    period_downloads: int | None = None
    rating: str | None = None
    created_at: datetime
    updated_at: datetime
    last_indexed_at: datetime | None = None


class SkillListResponse(BaseModel):
    skills: list[SkillResponse]
    total: int
    skip: int
    limit: int


class SkillSearchResult(BaseModel):
    skill: SkillResponse
    score: float | None = None


class RiskSignalSchema(BaseModel):
    id: str
    name: str
    description: str
    severity: str
    data: dict[str, Any] = Field(default_factory=dict)


class SecurityAuditResponse(BaseModel):
    id: str
    resource_type: str
    resource_id: str
    version: str | None = None
    commit_id: str | None = None
    audit_type: str
    risk_level: str
    risk_score: int | None = None
    risk_signals: list[RiskSignalSchema]
    details: dict[str, Any]
    audited_at: datetime


class AuditReportStats(BaseModel):
    """风险条数统计（按展示分组）。"""

    high: int = 0
    medium: int = 0
    low: int = 0
    total: int = 0


class AuditReportDimensionRef(BaseModel):
    """四大类下属的风险维度引用（层级关系：分类 -> 17 个维度）。"""

    key: str
    name: str


class AuditReportCategory(BaseModel):
    """风险分类聚合（四类之一）。"""

    key: str
    name: str
    description: str
    dimension_count: int
    dimensions: list[AuditReportDimensionRef] = Field(default_factory=list)
    stats: AuditReportStats


class AuditReportRuleRow(BaseModel):
    """维度下的检测项聚合（“风险检测详情”的行：检测项 -> 命中项）。"""

    rule_id: str
    rule_name: str | None = None
    stats: AuditReportStats
    max_severity_group: str
    remediation: str | None = None


class AuditReportDimension(BaseModel):
    """按风险维度聚合的命中项（“风险检测详情”的聚合行）。"""

    key: str
    name: str
    category_key: str
    description: str
    rule_ids: list[str] = Field(default_factory=list)
    rules: list[AuditReportRuleRow] = Field(default_factory=list)
    stats: AuditReportStats
    max_severity_group: str
    remediation: str | None = None


class AuditReportLocation(BaseModel):
    file: str | None = None
    start_line: int | None = None
    end_line: int | None = None


class AuditReportFinding(BaseModel):
    """一条风险明细（“风险检测详情”展开后的 issue 行，文案为中文）。"""

    id: str
    rule_id: str
    rule_name: str | None = None
    title: str
    dimension: str
    dimension_key: str
    category_key: str
    severity: str
    severity_group: str
    status: str
    remediation: str | None = None
    location: AuditReportLocation | None = None
    code_snippet: str | None = None
    truncated: bool = False


class AuditReportResponse(BaseModel):
    """Skill 风险评估报告（详情页“查看风险评估报告”数据源）。

    ``has_report`` 为 False 时 ``reason`` 说明原因（no_audit / report_unavailable），
    其余字段为初始值。
    """

    skill_id: str
    skill_name: str
    source_url: str | None = None
    repo_url: str | None = None
    version: str | None = None
    has_report: bool = False
    reason: str | None = None
    generated_at: datetime | None = None
    engine: str | None = None
    engine_version: str | None = None
    score: int | None = None
    level: str | None = None
    level_label: str | None = None
    level_description: str | None = None
    recommendation: str | None = None
    summary: str | None = None
    # 规则目录规模（“检测项”总数，静态值，与命中项数无关）
    rule_count: int = 0
    stats: AuditReportStats | None = None
    categories: list[AuditReportCategory] = Field(default_factory=list)
    dimensions: list[AuditReportDimension] = Field(default_factory=list)
    findings: list[AuditReportFinding] = Field(default_factory=list)
    findings_truncated: bool = False


class AuditByUrlRequest(BaseModel):
    """One-off security audit for a skill repository URL or a SKILL.md URL.

    Either ``repo_url`` (scan the whole repository) or ``skill_url`` (a
    ``<host>/<owner>/<repo>/blob/<ref>/<path>/SKILL.md`` link) must be provided.
    """

    repo_url: str | None = Field(
        None, max_length=2048, description="Git repository URL to scan (whole repo)"
    )
    branch: str | None = Field(
        None, max_length=255, description="Git ref/branch to scan (default: main)"
    )
    skill_url: str | None = Field(
        None, max_length=2048, description="SKILL.md blob URL to scan (single skill)"
    )
    scanners: str | None = Field(
        None, description="Comma-separated scanner list (default: skillspector)"
    )
    async_mode: bool = Field(
        False, description="Trigger scan without waiting for the result"
    )


class AuditByUrlResponse(BaseModel):
    """Result of a one-off audit-by-URL scan (not persisted)."""

    git_url: str
    ref: str
    skill_path: str
    risk_level: str
    risk_score: int | None = None
    risk_signals: list[RiskSignalSchema] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class AuditByUrlResultResponse(BaseModel):
    """Polling result of an async audit-by-URL scan (not persisted).

    ``status`` is one of ``pending`` / ``done`` / ``error``.  Only when
    ``status == "done"`` are ``risk_level`` / ``risk_score`` /
    ``risk_signals`` / ``details`` populated.
    """

    status: str
    build_number: int
    jenkins_status: str | None = None
    risk_level: str | None = None
    risk_score: int | None = None
    risk_signals: list[RiskSignalSchema] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None


class SkillVersionsResponse(BaseModel):
    source_url: str
    skill_id: str
    versions: list[SkillResponse]


class ContributorSkillsResponse(BaseModel):
    """Contributor（source + author）下的全部 skills 聚合视图。"""

    source: str
    author: str
    platform: str | None = None
    name: str | None = None
    description: str | None = None
    git_profile: str | None = None
    website: str | None = None
    skill_count: int
    total_downloads: int
    skills: list[SkillResponse]
    total: int
    skip: int
    limit: int


class ContributorResponse(BaseModel):
    """贡献者列表中的单项概要。"""

    id: str
    source: str
    author: str
    platform: str | None = None
    name: str | None = None
    description: str | None = None
    avatar_url: str | None = None
    git_profile: str | None = None
    website: str | None = None
    repo_url: str | None = None
    skill_count: int
    total_downloads: int = 0
    created_at: datetime
    updated_at: datetime


class ContributorListResponse(BaseModel):
    contributors: list[ContributorResponse]
    total: int
    grand_total: int = 0
    skip: int
    limit: int
    platform_counts: dict[str, int] = Field(default_factory=dict)


SkillVersionResponse = SkillVersionsResponse
