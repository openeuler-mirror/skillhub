"""Finding parsing, structural verdicts, scoring and ordering."""

from __future__ import annotations

import math
import re
from typing import Any

import yaml  # type: ignore[import-untyped]

from src.security.audit_review.constants import (
    _CAPABILITY_IN_EXPLANATION_RE,
    _PERMISSION_TO_CAPABILITY,
    _TOOL_TO_CAPABILITY,
    DEFAULT_CONFIDENCE,
    DIMINISHING_WEIGHTS,
    LEVEL_THRESHOLDS,
    MAX_OCCURRENCES_PER_RULE,
    RECOMMENDATION_BY_LEVEL,
    SEVERITY_POINTS,
)


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + " ...[truncated]"


def _issue_rule_id(issue: Any) -> str:
    if not isinstance(issue, dict):
        return ""
    return str(issue.get("id") or "").strip().upper()


def _issue_location(issue: Any) -> dict[str, Any]:
    if not isinstance(issue, dict):
        return {}
    location = issue.get("location")
    return location if isinstance(location, dict) else {}


def _issue_line(issue: Any) -> int | None:
    return _coerce_int(_issue_location(issue).get("start_line"))


def _issue_severity(issue: Any) -> str:
    if not isinstance(issue, dict):
        return "LOW"
    severity = str(issue.get("severity") or "").strip().upper()
    return severity if severity in SEVERITY_POINTS else "LOW"


def _false_positive_threshold(issue: Any, base: float, high: float) -> float:
    """误报删除门槛：HIGH/CRITICAL 使用更严格的值。"""
    if base <= 0 and high <= 0:
        return 0.0
    if high > 0 and _issue_severity(issue) in {"HIGH", "CRITICAL"}:
        return high
    return base


def _parse_manifest_frontmatter(content: str | None) -> dict[str, Any] | None:
    """解析 SKILL.md 的 YAML frontmatter。

    - 没有 frontmatter：返回 ``{}``（可以确定没有声明）
    - 有 frontmatter 但解析失败：返回 ``None``（调用方回退到 LLM 判定）
    """
    text = (content or "").lstrip("\ufeff")
    if not text.startswith("---"):
        return {}
    end_match = re.search(r"\n---\s*\n", text[3:])
    if not end_match:
        return None
    try:
        data = yaml.safe_load(text[3 : end_match.start() + 3])
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _manifest_field_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _manifest_declarations(manifest: dict[str, Any]) -> list[str]:
    values: list[str] = []
    values.extend(_manifest_field_values(manifest.get("permissions")))
    values.extend(_manifest_field_values(manifest.get("allowed-tools")))
    return [value for value in values if value.strip()]


def _declared_capabilities(manifest: dict[str, Any]) -> set[str]:
    """把 permissions / allowed-tools 映射成能力分类。

    同时支持列表、逗号分隔和空格分隔；``Bash(man:*)`` 这类带作用域
    的写法按 ``(`` 前的工具名识别，避免上游解析缺陷导致误判。
    """
    declared: set[str] = set()
    for value in _manifest_declarations(manifest):
        for token in re.split(r"[,\s]+", value):
            token = token.strip()
            if not token:
                continue
            base = token.split("(", 1)[0].strip().lower()
            category = _TOOL_TO_CAPABILITY.get(base)
            if category:
                declared.add(category)
            lowered = token.lower()
            for keyword, cap in _PERMISSION_TO_CAPABILITY.items():
                if re.search(rf"\b{re.escape(keyword)}\b", lowered):
                    declared.add(cap)
    return declared


def _structural_verdict(issue: Any, manifest: dict[str, Any]) -> dict[str, Any] | None:
    """用 SKILL.md 声明静态判定 LP1/LP3；无法判定时返回 ``None``。"""
    rule_id = _issue_rule_id(issue)
    if rule_id == "LP3":
        if _manifest_declarations(manifest):
            return {
                "is_vulnerability": False,
                "confidence": 1.0,
                "reason": (
                    "SKILL.md declares permissions/allowed-tools; "
                    "LP3 requires an absent declaration."
                ),
            }
        return {
            "is_vulnerability": True,
            "confidence": 1.0,
            "reason": "SKILL.md does not declare permissions/allowed-tools; LP3 is valid.",
        }
    if rule_id == "LP1":
        explanation = str(issue.get("explanation") or "") if isinstance(issue, dict) else ""
        match = _CAPABILITY_IN_EXPLANATION_RE.search(explanation)
        if not match:
            return None
        capability = match.group(1).strip().lower()
        if not capability:
            return None
        if capability in _declared_capabilities(manifest):
            return {
                "is_vulnerability": False,
                "confidence": 1.0,
                "reason": f"SKILL.md declares the '{capability}' capability; LP1 is a false positive.",
            }
        return {
            "is_vulnerability": True,
            "confidence": 1.0,
            "reason": f"SKILL.md does not declare the '{capability}' capability; LP1 is valid.",
        }
    return None


def _issue_confidence(issue: Any) -> float:
    """与上游 ``Finding.confidence`` 一致：缺省 0.5，取值裁剪到 [0, 1]。"""
    if not isinstance(issue, dict):
        return DEFAULT_CONFIDENCE
    raw = issue.get("confidence")
    if raw is None or isinstance(raw, bool):
        return DEFAULT_CONFIDENCE
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE
    if not math.isfinite(value):
        return DEFAULT_CONFIDENCE
    return max(0.0, min(1.0, value))


def compute_skillspector_score(issues: list[Any], has_executable_scripts: bool) -> int:
    """完全复刻 SkillSpector 原生风险分算法（0-100）。

    上游实现见 ``skillspector/nodes/report.py:_compute_risk_score``：
    按规则 ID 统计出现次数，前 3 次分别以 1 / 0.5 / 0.25 权重计入
    ``severity_points * weight * confidence``；confidence<=0 的命中跳过；
    存在可执行脚本时总分 ×1.3；最终 ``int()`` 截断并封顶 100。
    """
    rule_occurrences: dict[str, int] = {}
    score = 0.0
    for issue in issues:
        confidence = _issue_confidence(issue)
        if confidence <= 0:
            continue
        base_points = SEVERITY_POINTS[_issue_severity(issue)]
        rule_id = _issue_rule_id(issue) or "UNKNOWN"
        count = rule_occurrences.get(rule_id, 0)
        rule_occurrences[rule_id] = count + 1
        if count >= MAX_OCCURRENCES_PER_RULE:
            continue
        score += base_points * DIMINISHING_WEIGHTS[count] * confidence
    if has_executable_scripts:
        score *= 1.3
    return int(min(100.0, max(0.0, score)))


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool | None:
    """把模型返回的真假值收敛成 bool；无法识别时返回 None（保留命中）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "y", "1", "vulnerability", "vulnerable", "tp", "true_positive"}:
        return True
    if text in {
        "false",
        "no",
        "n",
        "0",
        "safe",
        "benign",
        "fp",
        "false_positive",
    }:
        return False
    return None


def _coerce_confidence(value: Any) -> float | None:
    """把模型返回的置信度收敛到 [0, 1]；支持 0.85 / "0.85" / "85%" 三种写法。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        percent = text.endswith("%")
        if percent:
            text = text[:-1].strip()
        try:
            number = float(text)
        except ValueError:
            return None
        if percent:
            number /= 100.0
    if not math.isfinite(number):
        return None
    return max(0.0, min(1.0, number))


def _as_score(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return max(0, min(100, int(round(number))))


def level_from_score(score: int | None) -> str:
    if score is None:
        return "unknown"
    for upper, level in LEVEL_THRESHOLDS:
        if score <= upper:
            return level
    return "critical"


def recommendation_for_level(level: str) -> str:
    return RECOMMENDATION_BY_LEVEL.get(level, "CAUTION")
