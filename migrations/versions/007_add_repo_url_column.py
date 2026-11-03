"""Add repo_url column to skills and skill_versions tables.

Revision ID: 007_add_repo_url_column
Revises: 006_rename_to_risk_score
Create Date: 2026-07-22 00:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "007_add_repo_url_column"
down_revision: str | None = "006_rename_to_risk_score"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    result = bind.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    if not column_exists("skills", "repo_url"):
        op.add_column("skills", sa.Column("repo_url", sa.Text(), nullable=True))
    if not column_exists("skill_versions", "repo_url"):
        op.add_column("skill_versions", sa.Column("repo_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("skills", "repo_url")
    op.drop_column("skill_versions", "repo_url")
