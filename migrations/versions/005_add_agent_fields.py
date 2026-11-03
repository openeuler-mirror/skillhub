"""empty migration for chain continuity

Revision ID: 005_add_agent_fields
Revises: 004_skill_repos
Create Date: 2026-06-28 00:00:00

"""
from collections.abc import Sequence

revision: str = "005_add_agent_fields"
down_revision: str | None = "004_skill_repos"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
