"""Skill 完备性判定：必填字段齐全才暴露给前端（is_complete=True）。

必填字段集中在此维护，后续新增必填项（如 description、logo_url 等）只需在
``COMPLETE_REQUIRED_FIELDS`` 追加。相关 SQL 回填位置（分类回填、安全审计回填）
均以注释引用此清单。
"""
from __future__ import annotations

from typing import Any, Mapping

# 必填字段：值为 None 视为未完善；字符串字段额外要求非空串。
COMPLETE_REQUIRED_FIELDS: tuple[str, ...] = ("risk_score", "category")


def is_skill_complete(fields: Mapping[str, Any]) -> bool:
    """字段值映射是否满足全部必填项。"""
    for field in COMPLETE_REQUIRED_FIELDS:
        value = fields.get(field)
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
    return True