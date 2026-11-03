"""Shared constants for the SkillSpector LLM review."""

from __future__ import annotations

import re

# SkillSpector 原生评分参数（与上游 nodes/report.py:_compute_risk_score 一致）：
# 同一规则前 3 次命中分别按 1 / 0.5 / 0.25 递减计分，第 4 次起不再计分；
# 存在可执行脚本时总分 ×1.3；最终 int() 截断并封顶 100。
SEVERITY_POINTS = {"CRITICAL": 50, "HIGH": 25, "MEDIUM": 10, "LOW": 5}
MAX_OCCURRENCES_PER_RULE = 3
DIMINISHING_WEIGHTS = (1.0, 0.5, 0.25)
DEFAULT_CONFIDENCE = 0.5

# 分数分档 -> 数据库 risk_level（与集采/前端展示口径一致）
LEVEL_THRESHOLDS = ((20, "low"), (50, "medium"), (80, "high"))

RECOMMENDATION_BY_LEVEL = {
    "low": "SAFE",
    "medium": "CAUTION",
    "high": "DO_NOT_INSTALL",
    "critical": "DO_NOT_INSTALL",
}

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_TRANSIENT_ATTEMPTS = 3
_MAX_SKILL_CONTEXT_CHARS = 2000
_MAX_SNIPPET_CHARS = 800
_MAX_EXPLANATION_CHARS = 300
_MAX_REASON_CHARS = 300
_MAX_FALSE_POSITIVE_DETAILS = 100
_RULE_ID_RE = re.compile(r"\b([A-Z]{2}\d+)\b")
# 部分审计命中数可能远超 100，误报理由只为展示做截断；
# 判定所需的 confidence 必须单独为全部误报保留，不能跟着理由一起截断。

# MCP 最小权限类规则只取决于 SKILL.md 的声明，不需要语义判断。
# 直接按 manifest 静态判定，避免小模型编造“已声明 allowed-tools / 未调用工具”
# 之类的理由把真阳性误删。
_STRUCTURAL_RULES = frozenset({"LP1", "LP3"})
_CAPABILITY_IN_EXPLANATION_RE = re.compile(r"uses '([^']+)' capability", re.IGNORECASE)
# 与 SkillSpector mcp_least_privilege 分析器保持一致的工具/权限映射
_TOOL_TO_CAPABILITY = {
    "bash": "shell",
    "execute": "shell",
    "terminal": "shell",
    "read": "file_read",
    "glob": "file_read",
    "ls": "file_read",
    "write": "file_write",
    "edit": "file_write",
    "multiedit": "file_write",
    "notebookedit": "file_write",
    "webfetch": "network",
    "websearch": "network",
    "fetch": "network",
    "env": "env",
}
_PERMISSION_TO_CAPABILITY = {
    "bash": "shell",
    "shell": "shell",
    "terminal": "shell",
    "command": "shell",
    "network": "network",
    "http": "network",
    "fetch": "network",
    "api": "network",
    "read": "file_read",
    "fs_read": "file_read",
    "file_read": "file_read",
    "write": "file_write",
    "fs_write": "file_write",
    "file_write": "file_write",
    "env": "env",
    "environment": "env",
    "mcp": "mcp",
    "tools": "mcp",
    "tool_use": "mcp",
}

# 复核提示词/判定结构版本；升级后提前写入的 partial 记录会从头复核
PROMPT_VERSION = 5
# 模型未返回 confidence 时的默认值（与上游 meta_analyzer 一致），
# 避免因字段缺失把真阳性误判为低置信度而删除
DEFAULT_VERDICT_CONFIDENCE = 0.7
