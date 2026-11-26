import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    desc,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class SkillRepoModel(Base):
    __tablename__ = "skill_repos"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    platform: Mapped[str | None] = mapped_column(String(100), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    repository_commit_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    skill_discover_status: Mapped[str] = mapped_column(String(50), nullable=False, default="init")
    skill_num: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stars_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    forks_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    watchers_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    popularity_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_soft_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    skills: Mapped[list["Skill"]] = relationship(
        back_populates="skill_repo",
        cascade="all, delete-orphan",
    )
    skill_versions: Mapped[list["SkillVersion"]] = relationship(
        back_populates="skill_repo",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_skill_repos_source", "source"),
        Index("idx_skill_repos_platform", "platform"),
        Index("idx_skill_repos_status", "skill_discover_status"),
        Index("idx_skill_repos_created_at", desc("created_at")),
        Index("idx_skill_repos_author", "author"),
    )


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    skill_repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("skill_repos.id", ondelete="CASCADE"),
        nullable=False,
    )
    skill_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    commit_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tree_hash: Mapped[str | None] = mapped_column(String(40), nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(100), nullable=True)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # license 类型：归一化后的 SPDX 表达式（如 ``MIT`` / ``MIT AND Apache-2.0``）。
    # 由 discover 扫描内的 license 预检维护；
    # NULL 表示尚未检查，或检查后无法识别出具体 license（如 missing）。
    license: Mapped[str | None] = mapped_column(Text, nullable=True)
    # license 合规三态（由 discover 预检维护）：
    #   1    = 已检查且满足 license 要求
    #   0    = 已检查但不满足
    #   NULL = 尚未检查或检查失败（等待下一轮扫描回填）
    # 当前仅作为字段记录，不参与列表/搜索/下载等可见性过滤。
    license_compliant: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[str | None] = mapped_column(String(10), nullable=True)
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    is_soft_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(768), nullable=True)

    skill_repo: Mapped["SkillRepoModel"] = relationship(back_populates="skills")

    __table_args__ = (
        Index("idx_skills_skill_repo_id", "skill_repo_id"),
        Index("idx_skills_category", "category"),
        Index("idx_skills_platform", "platform"),
        Index("idx_skills_source", "source"),
        Index("idx_skills_created_at", desc("created_at")),
        Index("idx_skills_tags", "tags", postgresql_using="gin"),
        UniqueConstraint("skill_id", name="uq_skills_skill_id"),
        Index("idx_skills_unique", "skill_id", unique=True),
    )


class SkillVersion(Base):
    __tablename__ = "skill_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    skill_repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("skill_repos.id", ondelete="CASCADE"),
        nullable=False,
    )
    skill_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    commit_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tree_hash: Mapped[str | None] = mapped_column(String(40), nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(100), nullable=True)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # license 类型：归一化后的 SPDX 表达式（如 ``MIT`` / ``MIT AND Apache-2.0``）。
    # 与 skills 表保持一致，便于版本列表直接展示该版本实际使用的 license。
    license: Mapped[str | None] = mapped_column(Text, nullable=True)
    # license 合规三态（由 discover 预检维护）：
    #   1    = 已检查且满足 license 要求
    #   0    = 已检查但不满足
    #   NULL = 尚未检查或检查失败（等待下一轮扫描回填）
    # 当前仅作为字段记录，不参与版本列表等可见性过滤。
    license_compliant: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(768), nullable=True)

    skill_repo: Mapped["SkillRepoModel"] = relationship(back_populates="skill_versions")

    __table_args__ = (
        Index("idx_skill_versions_skill_repo_id", "skill_repo_id"),
        Index("idx_skill_versions_category", "category"),
        Index("idx_skill_versions_platform", "platform"),
        Index("idx_skill_versions_source", "source"),
        Index("idx_skill_versions_created_at", desc("created_at")),
        Index("idx_skill_versions_tags", "tags", postgresql_using="gin"),
        Index("idx_skill_versions_skill_id", "skill_id"),
        Index("idx_skill_versions_unique", "skill_id", "version", unique=True),
    )


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    commit_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    risk_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 新增字段
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    homepage_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    license: Mapped[str | None] = mapped_column(String(50), nullable=True)
    readme_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_yaml_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    supported_platforms: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    verified: Mapped[bool] = mapped_column(default=False)
    star_count: Mapped[int] = mapped_column(Integer, default=0)
    contributor_count: Mapped[int] = mapped_column(Integer, default=0)
    latest_commit_id: Mapped[str | None] = mapped_column(String(40), nullable=True)

    versions: Mapped[list["AgentVersion"]] = relationship(back_populates="agent", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_agents_category", "category"),
        Index("idx_agents_tags", "tags", postgresql_using="gin"),
        Index("idx_agents_source", "source"),
        Index("idx_agents_verified", "verified"),
    )


class AgentVersion(Base):
    __tablename__ = "agent_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[str] = mapped_column(String(255), nullable=False)
    commit_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    agent: Mapped["Agent"] = relationship(back_populates="versions")

    __table_args__ = (
        Index("idx_agent_versions_agent_id", "agent_id"),
    )


class SecurityAudit(Base):
    __tablename__ = "security_audits"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resource_type: Mapped[str] = mapped_column(String(20), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    commit_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    audit_type: Mapped[str] = mapped_column(String(50), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    risk_signals: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    audited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_audits_resource", "resource_type", "resource_id"),
        Index("idx_audits_risk_level", "risk_level"),
        Index("idx_audits_audited_at", desc("audited_at")),
        Index("idx_audits_version", "resource_id", "version", "commit_id"),
    )


class DownloadHistory(Base):
    __tablename__ = "download_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resource_type: Mapped[str] = mapped_column(String(20), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_downloads_resource", "resource_type", "resource_id"),
        Index("idx_downloads_date", desc("downloaded_at")),
    )


class Contributor(Base):
    __tablename__ = "contributors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    author: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str | None] = mapped_column(String(100), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    git_profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("source", "author", name="uq_contributors_source_author"),
        Index("idx_contributors_source", "source"),
        Index("idx_contributors_platform", "platform"),
        Index("idx_contributors_skill_count", desc("skill_count")),
        Index("idx_contributors_created_at", desc("created_at")),
    )
