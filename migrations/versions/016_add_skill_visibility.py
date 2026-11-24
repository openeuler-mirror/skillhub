"""Add skill visibility flags: is_complete (completeness) + is_soft_deleted (soft-delete).

design
------
* ``skills.is_complete`` 默认 false，当必填字段（risk_score + category，见
  ``src/skills/skill_readiness.py``）齐全、且 license 未判为不合规
  （``license_compliant != 0``，即 1 合规或 NULL 未检查）时置 true
* ``is_soft_deleted``（skills 与 skill_repos）默认 false，catalog 下架时置 true（软删除）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "016_add_skill_visibility"
down_revision: str | None = "015_add_license_compliant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "skills",
        sa.Column("is_complete", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "skills",
        sa.Column("is_soft_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "skill_repos",
        sa.Column("is_soft_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    # 存量行回填：字段已完备的 skill 置为 is_complete。完备口径与
    # src/skills/skill_readiness.is_skill_complete 一致：risk_score、category
    # 均非空，且 license_compliant 未判为不合规（NULL 未检查不拦截）。
    # 本迁移紧随 015 添加 license_compliant，存量全为 NULL，`IS DISTINCT FROM 0`
    # 对 NULL 返回 true，不会改变回填结果；仅保证口径与判定函数一致。
    op.execute(
        "UPDATE skills SET is_complete = "
        "(risk_score IS NOT NULL AND category IS NOT NULL AND category <> '' "
        "AND license_compliant IS DISTINCT FROM 0)"
    )


def downgrade() -> None:
    op.drop_column("skill_repos", "is_soft_deleted")
    op.drop_column("skills", "is_soft_deleted")
    op.drop_column("skills", "is_complete")