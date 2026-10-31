"""Add popularity columns to skill_repos.

Revision ID: 011_add_repo_popularity
Revises: 010_add_summary_search_index
Create Date: 2026-08-07 00:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "011_add_repo_popularity"
down_revision: str | None = "010_add_summary_search_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "skill_repos",
        sa.Column("stars_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "skill_repos",
        sa.Column("forks_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "skill_repos",
        sa.Column("watchers_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "skill_repos",
        sa.Column(
            "popularity_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("skill_repos", "popularity_updated_at")
    op.drop_column("skill_repos", "watchers_count")
    op.drop_column("skill_repos", "forks_count")
    op.drop_column("skill_repos", "stars_count")
