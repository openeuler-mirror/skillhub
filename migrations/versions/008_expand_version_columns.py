"""Expand version columns to support long Git tag names.

Revision ID: 008_expand_version_columns
Revises: 007_add_repo_url_column
Create Date: 2026-08-06 00:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "008_expand_version_columns"
down_revision: str | None = "007_add_repo_url_column"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


VERSION_TABLES = (
    "skills",
    "skill_versions",
    "agents",
    "agent_versions",
    "security_audits",
)


def upgrade() -> None:
    for table_name in VERSION_TABLES:
        op.alter_column(
            table_name,
            "version",
            existing_type=sa.String(length=50),
            type_=sa.String(length=255),
            existing_nullable=table_name != "agent_versions",
        )


def downgrade() -> None:
    # PostgreSQL will reject this downgrade if any stored version exceeds 50
    # characters, preventing silent truncation.
    for table_name in reversed(VERSION_TABLES):
        op.alter_column(
            table_name,
            "version",
            existing_type=sa.String(length=255),
            type_=sa.String(length=50),
            existing_nullable=table_name != "agent_versions",
        )
