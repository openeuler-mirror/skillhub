"""Add skill repos table and skill repo foreign key.

Revision ID: 002_skill_repos
Revises: 001_initial_schema
Create Date: 2026-06-06 00:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002_skill_repos"
down_revision: str | None = "001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "skill_repos",
        sa.Column("id", UUID, primary_key=True, nullable=False, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("repo_name", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("platform", sa.String(length=100), nullable=True),
        sa.Column("branch", sa.String(length=255), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("repository_commit_id", sa.String(length=40), nullable=True),
        sa.Column("skill_discover_status", sa.String(length=50), nullable=False, server_default=sa.text("'init'")),
        sa.Column("skill_num", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("repo_name", name="uq_skill_repos_repo_name"),
    )

    op.create_index(
        "idx_skill_repos_source",
        "skill_repos",
        ["source"],
    )
    op.create_index(
        "idx_skill_repos_platform",
        "skill_repos",
        ["platform"],
    )
    op.create_index(
        "idx_skill_repos_status",
        "skill_repos",
        ["skill_discover_status"],
    )
    op.create_index(
        "idx_skill_repos_created_at",
        "skill_repos",
        [sa.text("created_at DESC")],
    )

    op.add_column(
        "skills",
        sa.Column("skill_repo_id", UUID, nullable=False),
    )
    op.add_column(
        "skill_versions",
        sa.Column("skill_repo_id", UUID, nullable=False),
    )
    op.create_foreign_key(
        "fk_skills_skill_repo_id",
        "skills",
        "skill_repos",
        ["skill_repo_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_skill_versions_skill_repo_id",
        "skill_versions",
        "skill_repos",
        ["skill_repo_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "idx_skills_skill_repo_id",
        "skills",
        ["skill_repo_id"],
    )
    op.create_index(
        "idx_skill_versions_skill_repo_id",
        "skill_versions",
        ["skill_repo_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_skill_versions_skill_repo_id", table_name="skill_versions")
    op.drop_index("idx_skills_skill_repo_id", table_name="skills")
    op.drop_constraint("fk_skill_versions_skill_repo_id", "skill_versions", type_="foreignkey")
    op.drop_constraint("fk_skills_skill_repo_id", "skills", type_="foreignkey")
    op.drop_column("skill_versions", "skill_repo_id")
    op.drop_column("skills", "skill_repo_id")

    op.drop_index(
        "idx_skill_repos_created_at",
        table_name="skill_repos",
    )
    op.drop_index(
        "idx_skill_repos_status",
        table_name="skill_repos",
    )
    op.drop_index(
        "idx_skill_repos_source",
        table_name="skill_repos",
    )
    op.drop_index(
        "idx_skill_repos_platform",
        table_name="skill_repos",
    )
    op.drop_table("skill_repos")
