"""为 skills / skill_versions 增加 license 合规三态标记。

背景
----
旧的 license 准入方案会在定时任务里**删除**不合规的 skill。本方案改为
“只标记、不删除”，在两张表上各增加一列 ``license_compliant``：

======  ==========================================================
取值      含义
======  ==========================================================
``1``    已检查，且满足 license 要求（判定为 allowed）
``0``    已检查，但不满足 license 要求（black/restricted/missing/unknown）
``NULL`` 尚未检查，或历史判定从未得到确定结论（如仓库持续不可达）
======  ==========================================================

为什么用 ``SMALLINT`` 而不是 ``BOOLEAN``
----------------------------------------
``BOOLEAN`` 只有真/假两态，无法表达“未检查”。三态是本方案的核心语义：
NULL 既表示“还没有结论”，也是定时任务下一轮补查的输入条件；同时 API 的
默认过滤只排除 0，NULL 仍对外可见（未检查不等于不合规）。

为什么两张表都加
----------------
``skill_versions`` 是独立的 tag 版本行，版本列表/下载接口直接读该表；
与 ``risk_score`` 一样保持两表字段平行，避免 tag 版本没有合规结论。

升级影响
--------
迁移完成后存量行全部为 NULL。由于 NULL 默认仍可见，升级不会导致存量
skill 从列表消失；每日 license 任务会逐步把 NULL 回填为 1/0。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "015_add_license_compliant"
down_revision: str | None = "014_add_skill_embedding_hnsw"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 两张表结构一致：先加可空列（避免对存量行做默认值回填），再加 CHECK
    # 约束限制取值范围。约束允许 NULL（PostgreSQL CHECK 对 NULL 视为通过），
    # 因此“未检查”不会被约束误伤。
    for table in ("skills", "skill_versions"):
        op.execute(
            f"ALTER TABLE {table} "
            "ADD COLUMN IF NOT EXISTS license_compliant SMALLINT NULL"
        )
        # 约束名带表名，长度在 PostgreSQL 的 63 字符限制内。
        # PostgreSQL 的 ADD CONSTRAINT 不支持 IF NOT EXISTS，先 DROP IF EXISTS
        # 再 ADD，保证 upgrade 在部分执行的库上也能重复执行。
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{table}_license_compliant"
        )
        op.execute(
            f"ALTER TABLE {table} "
            "ADD CONSTRAINT ck_{table}_license_compliant "
            "CHECK (license_compliant IN (0, 1))"
        )


def downgrade() -> None:
    # 先删约束再删列，保证 downgrade 在约束存在/不存在的库上都能幂等执行。
    for table in ("skills", "skill_versions"):
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{table}_license_compliant"
        )
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS license_compliant")
