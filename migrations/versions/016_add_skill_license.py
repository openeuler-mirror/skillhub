"""为 skills / skill_versions 增加 license 类型字段。

背景
----
``license_compliant`` 只记录三态结论（1/0/NULL），无法回答“这个 skill 到底
用的什么 license”。本迁移在两张表上各增加一个可空的 ``license`` 列，用于
保存判定引擎归一化后的 SPDX 表达式：

- 声明可解析：保存声明本身（如 ``MIT``、``MIT AND Apache-2.0``）；
- 无声明但能从 LICENSE 文件识别：保存识别结果（多个文件按 AND 拼接）；
- 未声明且无文件（missing）或暂时无法识别：保持 NULL。

为什么用 TEXT
-------------
前端声明可能是列表/复合表达式（``MIT AND Apache-2.0``），无法预估长度；
``VARCHAR(255)`` 存在截断/写入失败风险，因此使用不设长度的 TEXT。

为什么两张表都加
----------------
``skill_versions`` 是独立的 tag 版本行，版本列表/下载接口直接读该表；
与 ``license_compliant`` 保持两表字段平行，避免版本行没有 license 类型。

升级影响
--------
迁移完成后存量行全部为 NULL。随后：

- discover 在扫描新入库/内容变化的 skill 时做一次 license 预检并写入；
- license 定时任务在 discover 每轮完成后全量复核并补齐历史数据。

由 license 判定流程按 ``verdict.expression`` 回填，不需要在本迁移中做
不可靠的 JSONB 反解析（``extra_metadata.license`` 可能是列表或描述性文本）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "016_add_skill_license"
down_revision: str | None = "015_add_license_compliant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 可空列、不设默认值：避免对存量大表做整表回填与重写。
    # IF NOT EXISTS 保证在部分执行/重复执行的库上也能幂等升级。
    for table in ("skills", "skill_versions"):
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS license TEXT NULL"
        )


def downgrade() -> None:
    # 先删列即可：``license`` 上只有普通列定义，没有索引/约束依赖。
    # IF EXISTS 保证在未执行过 upgrade 或已手工删列的库上也能幂等回滚。
    for table in ("skills", "skill_versions"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS license")
