"""Skill 完备性判定：必填字段齐全才暴露给前端（is_complete=True）。

必填字段集中在此维护，后续新增必填项（如 description、logo_url 等）只需在
``COMPLETE_REQUIRED_FIELDS`` 追加。相关 SQL 回填位置（分类回填、安全审计回填）
均以注释引用此清单。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# 必填字段：值为 None 视为未完善；字符串字段额外要求非空串。
COMPLETE_REQUIRED_FIELDS: tuple[str, ...] = ("risk_score", "category")


def is_skill_complete(source: Mapping[str, Any] | object) -> bool:
    """判断 source 是否满足全部必填字段。

    source 可为字段映射（dict 等）或任意带对应属性的对象（如 Skill ORM 实例），
    按 COMPLETE_REQUIRED_FIELDS 逐字段取值：映射走 ``.get``，对象走 ``getattr``。
    值为 None 或空串均视为未完善。
    """
    for field in COMPLETE_REQUIRED_FIELDS:
        value = (
            source.get(field) if isinstance(source, Mapping) else getattr(source, field, None)
        )
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
    return True