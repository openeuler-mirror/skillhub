"""Add fields to Agent model and create AgentVersion model.

Revision ID: 003_add_agent_fields
Revises: 002_skill_repos
Create Date: 2026-07-14 00:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "003_add_agent_fields"
down_revision: str | None = "002_skill_repos"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
ARRAY_TEXT = postgresql.ARRAY(sa.String())


def upgrade() -> None:
    # Add new columns to agents table
    op.add_column("agents", sa.Column("commit_id", sa.String(length=40), nullable=True))
    op.add_column("agents", sa.Column("logo_url", sa.Text(), nullable=True))
    op.add_column("agents", sa.Column("homepage_url", sa.Text(), nullable=True))
    op.add_column("agents", sa.Column("license", sa.String(length=50), nullable=True))
    op.add_column("agents", sa.Column("readme_content", sa.Text(), nullable=True))
    op.add_column("agents", sa.Column("agent_yaml_content", sa.Text(), nullable=True))
    op.add_column("agents", sa.Column("parsed_config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("agents", sa.Column("supported_platforms", ARRAY_TEXT, nullable=True))
    op.add_column("agents", sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("agents", sa.Column("star_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("agents", sa.Column("contributor_count", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("agents", sa.Column("latest_commit_id", sa.String(length=40), nullable=True))

    # Create agent_versions table
    op.create_table(
        "agent_versions",
        sa.Column("id", UUID, primary_key=True, nullable=False, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("agent_id", UUID, sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("commit_id", sa.String(length=40), nullable=True),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("download_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )

    # Add indexes
    op.create_index(
        "idx_agent_versions_agent_id",
        "agent_versions",
        ["agent_id"],
    )
    op.create_index(
        "idx_agents_source",
        "agents",
        ["source"],
    )
    op.create_index(
        "idx_agents_verified",
        "agents",
        ["verified"],
    )


def downgrade() -> None:
    # Drop indexes
    op.drop_index("idx_agents_verified", table_name="agents")
    op.drop_index("idx_agents_source", table_name="agents")
    op.drop_index("idx_agent_versions_agent_id", table_name="agent_versions")

    # Drop agent_versions table
    op.drop_table("agent_versions")

    # Drop columns from agents table
    op.drop_column("agents", "latest_commit_id")
    op.drop_column("agents", "contributor_count")
    op.drop_column("agents", "star_count")
    op.drop_column("agents", "verified")
    op.drop_column("agents", "supported_platforms")
    op.drop_column("agents", "parsed_config")
    op.drop_column("agents", "agent_yaml_content")
    op.drop_column("agents", "readme_content")
    op.drop_column("agents", "license")
    op.drop_column("agents", "homepage_url")
    op.drop_column("agents", "logo_url")
    op.drop_column("agents", "commit_id")
