"""Add the default name and description search index.

Revision ID: 010_add_summary_search_index
Revises: 009_optimize_skill_search
Create Date: 2026-08-06 00:00:00

"""
from collections.abc import Sequence

from alembic import op

revision: str = "010_add_summary_search_index"
down_revision: str | None = "009_optimize_skill_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX idx_skills_search_summary_gin
        ON skills
        USING gin (
            to_tsvector(
                'zhcfg'::regconfig,
                coalesce(name, '') || ' ' || coalesce(description, '')
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_skills_search_summary_gin")
