"""Add contributors table and author column to skill_repos.

Revision ID: 013_contributors_git_profile
Revises: 012_add_skill_tree_hash
Create Date: 2026-09-15 00:00:00

contributors 表包含 git_profile（代码托管平台主页）和 website（官网地址）
两列，对应 skill.yaml 中的同名字段。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "013_contributors_git_profile"
down_revision: str | None = "012_add_skill_tree_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) skill_repos 加 author 列
    op.add_column(
        "skill_repos",
        sa.Column("author", sa.String(255), nullable=True),
    )
    op.create_index("idx_skill_repos_author", "skill_repos", ["author"])

    # 2) 新建 contributors 表
    op.create_table(
        "contributors",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("author", sa.String(255), nullable=False),
        sa.Column("platform", sa.String(100), nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("git_profile", sa.Text(), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("repo_url", sa.Text(), nullable=True),
        sa.Column("skill_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("source", "author", name="uq_contributors_source_author"),
    )
    op.create_index("idx_contributors_source", "contributors", ["source"])
    op.create_index("idx_contributors_platform", "contributors", ["platform"])
    op.create_index(
        "idx_contributors_skill_count",
        "contributors",
        [sa.text("skill_count DESC")],
    )
    op.create_index(
        "idx_contributors_created_at",
        "contributors",
        [sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_table("contributors")
    op.drop_index("idx_skill_repos_author", table_name="skill_repos")
    op.drop_column("skill_repos", "author")
