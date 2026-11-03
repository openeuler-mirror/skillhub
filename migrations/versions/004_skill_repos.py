"""empty migration for chain continuity

Revision ID: 004_skill_repos
Revises: 003_add_agent_fields
Create Date: 2026-06-20 00:00:00

"""
from collections.abc import Sequence

revision: str = "004_skill_repos"
down_revision: str | None = "003_add_agent_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
