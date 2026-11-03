"""Add tree_hash column to skills and skill_versions tables.

Revision ID: 012_add_skill_tree_hash
Revises: 011_add_repo_popularity
Create Date: 2026-08-20 00:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "012_add_skill_tree_hash"
down_revision: str | None = "011_add_repo_popularity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "skills",
        sa.Column("tree_hash", sa.String(40), nullable=True),
    )
    op.add_column(
        "skill_versions",
        sa.Column("tree_hash", sa.String(40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("skills", "tree_hash")
    op.drop_column("skill_versions", "tree_hash")
