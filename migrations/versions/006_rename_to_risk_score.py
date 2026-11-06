"""empty migration for chain continuity

Revision ID: 006_rename_to_risk_score
Revises: 005_add_agent_fields
Create Date: 2026-07-05 00:00:00

"""
from collections.abc import Sequence

revision: str = "006_rename_to_risk_score"
down_revision: str | None = "005_add_agent_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
