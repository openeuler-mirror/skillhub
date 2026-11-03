"""Add indexes used by skill text search.

Revision ID: 009_optimize_skill_search
Revises: 008_expand_version_columns
Create Date: 2026-08-06 00:00:00

"""
from collections.abc import Sequence

from alembic import op

revision: str = "009_optimize_skill_search"
down_revision: str | None = "008_expand_version_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        CREATE INDEX idx_skills_search_text_gin
        ON skills
        USING gin (
            to_tsvector(
                'zhcfg'::regconfig,
                coalesce(name, '') || ' ' ||
                coalesce(description, '') || ' ' ||
                coalesce(content, '')
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_skills_name_trgm "
        "ON skills USING gin (name gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX idx_skills_description_trgm "
        "ON skills USING gin (description gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_skills_description_trgm")
    op.execute("DROP INDEX IF EXISTS idx_skills_name_trgm")
    op.execute("DROP INDEX IF EXISTS idx_skills_search_text_gin")
