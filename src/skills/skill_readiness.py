"""Skill 完备性判定：必填字段齐全且 license 未被判为不合规才暴露（is_complete=True）。

必填字段（risk_score、category）集中在此维护，后续新增必填项（如 description、
logo_url 等）只需在 ``COMPLETE_REQUIRED_FIELDS`` 追加。license_compliant 是
三态标记，判定规则独立于必填字段（见 is_skill_complete）。相关 SQL 回填位置
（分类回填、安全审计回填、license 联动刷新）需同时引用本清单与 license 判定。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# 必填字段：值为 None 视为未完善；字符串字段额外要求非空串。
# license_compliant 不在此列：它是三态标记（1 合规 / 0 不合规 / NULL 未检查），
# 判定规则不同（见 is_skill_complete），不能套用“None 视为未完善”的通用规则。
COMPLETE_REQUIRED_FIELDS: tuple[str, ...] = ("risk_score", "category")


def is_skill_complete(source: Mapping[str, Any] | object) -> bool:
    """判断 source 是否字段完备（决定对外展示/搜索口径）。

    source 可为字段映射（dict 等）或任意带对应属性的对象（如 Skill ORM 实例），
    按 COMPLETE_REQUIRED_FIELDS 逐字段取值：映射走 ``.get``，对象走 ``getattr``；
    值为 None 或空串均视为未完善。

    除必填字段外，license 合规三态也参与判定：``license_compliant == 0``
    （已检查但不合规）视为不完备；``1``（合规）与 ``None``（未检查）不额外
    拦截，避免未检查的 skill 被误隐藏。
    """
    for field in COMPLETE_REQUIRED_FIELDS:
        value = (
            source.get(field) if isinstance(source, Mapping) else getattr(source, field, None)
        )
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
    license_compliant = (
        source.get("license_compliant")
        if isinstance(source, Mapping)
        else getattr(source, "license_compliant", None)
    )
    if license_compliant == 0:
        return False
    return True