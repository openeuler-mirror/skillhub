"""Tests for the aggregated risk assessment report (``GET /skills/{id}/audit-report``).

Covers the read-time aggregation of the SkillSpector report stashed in
``security_audits.details`` and the route wiring (404 / no-audit / with-report).
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.api.routes.skills import get_skill_audit_report
from src.api.services import audit_report as audit_report_module
from src.api.services.audit_report import build_audit_report, resolve_dimension
from src.api.services.skillspector_rules import RULE_CATALOG


def _skill(**overrides):
    skill = MagicMock()
    skill.id = "skill-uuid"
    skill.skill_id = "github:openeuler/find-skills/find-skills"
    skill.name = "find-skills"
    skill.source_url = "https://atomgit.com/openeuler/find-skills/blob/main/SKILL.md"
    skill.repo_url = "https://atomgit.com/openeuler/find-skills"
    skill.version = "1.0.0"
    skill.risk_score = 12
    for key, value in overrides.items():
        setattr(skill, key, value)
    return skill


def _issue(
    rule_id: str,
    category: str | None,
    severity: str,
    explanation: str = "explanation",
    remediation: str | None = "remediation",
    code_snippet: str | None = "os.system('x')",
    file: str = "src/main.py",
    start_line: int = 1,
):
    return {
        "id": rule_id,
        "category": category,
        "severity": severity,
        "explanation": explanation,
        "remediation": remediation,
        "code_snippet": code_snippet,
        "location": {"file": file, "start_line": start_line, "end_line": start_line + 2},
    }


def _audit(issues: list[dict], score: int | None = 12, **details_overrides):
    details = {
        "skillspector_score": score,
        "skillspector_version": "2.4.1",
        "recommendation": "SAFE",
        "skillspector_report": {"risk_assessment": {"score": score}, "issues": issues},
    }
    details.update(details_overrides)
    audit = MagicMock()
    audit.details = details
    audit.audited_at = datetime(2026, 8, 22, 14, 32, tzinfo=UTC)
    return audit


# ---------------------------------------------------------------- builder


def test_build_report_without_audit():
    result = build_audit_report(_skill(), None)

    assert result["has_report"] is False
    assert result["reason"] == "no_audit"
    assert result["skill_id"] == "github:openeuler/find-skills/find-skills"
    assert result["findings"] == []
    assert result["categories"] == []
    # 无审计记录时同样输出检测项总数（前端元信息与分类卡计数使用）
    assert result["rule_count"] == len(RULE_CATALOG)


def test_build_report_without_parsed_report():
    audit = MagicMock()
    audit.details = {"skillspector_status": "FAILURE"}
    audit.audited_at = datetime(2026, 8, 22, tzinfo=UTC)

    result = build_audit_report(_skill(), audit)

    assert result["has_report"] is False
    assert result["reason"] == "report_unavailable"


def test_build_report_aggregates_categories_and_findings():
    issues = [
        _issue("P1", "Prompt Injection", "HIGH"),
        _issue("P2", "Prompt Injection", "LOW"),
        _issue("E1", "Data Exfiltration", "MEDIUM"),
        _issue("AST2", None, "CRITICAL"),
        _issue("SC1", "Supply Chain", "LOW"),
    ]
    result = build_audit_report(_skill(), _audit(issues, score=12))

    assert result["has_report"] is True
    assert result["score"] == 12
    assert result["level"] == "safe"
    assert result["level_label"] == "安全"
    assert result["engine"] == "NVIDIA SkillSpector"
    assert result["engine_version"] == "2.4.1"
    assert result["stats"] == {"high": 2, "medium": 1, "low": 2, "total": 5}
    # 检测项（规则目录规模）为静态值，与命中项数无关
    assert result["rule_count"] == 89

    categories = {category["key"]: category for category in result["categories"]}
    assert set(categories) == {"prompt", "data", "privilege_code", "supply_chain"}
    assert categories["prompt"]["stats"]["total"] == 2
    assert categories["data"]["stats"]["medium"] == 1
    # AST/污点流/YARA 等无 category 的分析器按规则 ID 归入权限与代码执行类
    assert categories["privilege_code"]["stats"]["high"] == 1
    assert categories["supply_chain"]["stats"]["low"] == 1
    # 维度数固定为 5 / 2 / 9 / 1，与设计稿一致
    assert [c["dimension_count"] for c in result["categories"]] == [5, 2, 9, 1]
    # 分类 -> 维度层级关系随分类一起返回
    assert [item["key"] for item in categories["prompt"]["dimensions"]] == [
        "prompt_injection",
        "system_prompt_leakage",
        "memory_poisoning",
        "anti_refusal",
        "trigger_abuse",
    ]
    assert categories["data"]["dimensions"][1] == {"key": "output_handling", "name": "输出处理"}

    findings = result["findings"]
    assert len(findings) == 5
    assert findings[0]["dimension"] == "提示注入"
    assert findings[0]["category_key"] == "prompt"
    assert findings[0]["severity_group"] == "high"
    assert findings[0]["status"] == "open"
    assert findings[0]["location"]["file"] == "src/main.py"
    # CRITICAL 与 HIGH 合并为高风险；AST 规则 → 危险代码语法
    ast_finding = next(item for item in findings if item["id"] == "AST2")
    assert ast_finding["dimension"] == "危险代码语法"
    assert ast_finding["category_key"] == "privilege_code"
    assert ast_finding["severity_group"] == "high"
    # 明细按严重度排序（高风险在前）
    assert [item["severity_group"] for item in findings[:2]] == ["high", "high"]


def test_build_report_localizes_texts_to_chinese():
    """风险项描述与修复建议使用 RISK_MODEL_SUMMARY.md 的中文文案，不透出英文原文。"""
    issues = [
        _issue(
            "AST4",
            None,
            "MEDIUM",
            explanation="subprocess module calls execute external commands.",
            remediation="Use subprocess.run() with shell=False.",
        )
    ]
    result = build_audit_report(_skill(), _audit(issues))
    finding = result["findings"][0]

    assert finding["rule_id"] == "AST4"
    assert finding["rule_name"] == "subprocess 执行"
    assert finding["title"].startswith("subprocess 执行：")
    assert "shell=False" in finding["remediation"]
    assert "external commands" not in finding["title"]
    assert "Use subprocess.run" not in finding["remediation"]
    # 维度描述为中文
    assert finding["title"].endswith("可直接执行任意代码或绕过静态检测")


def test_build_report_aggregates_dimensions():
    """按 17 个维度聚合命中项：规则 ID 列表、最高等级、中文建议合并。"""
    issues = [
        _issue("TT3", None, "LOW"),
        _issue("TT5", None, "HIGH"),
        _issue("P1", "Prompt Injection", "MEDIUM"),
    ]
    result = build_audit_report(_skill(), _audit(issues))

    dimensions = {item["key"]: item for item in result["dimensions"]}
    assert list(dimensions) == ["prompt_injection", "data_flow"]

    data_flow = dimensions["data_flow"]
    assert data_flow["name"] == "污点流"
    assert data_flow["category_key"] == "privilege_code"
    assert data_flow["rule_ids"] == ["TT3", "TT5"]
    assert data_flow["stats"] == {"high": 1, "medium": 0, "low": 1, "total": 2}
    assert data_flow["max_severity_group"] == "high"
    assert "绝不通过网络发送凭证" in data_flow["remediation"]
    assert "外部输入绝不经" in data_flow["remediation"]

    # 维度下的检测项聚合行（详情表按检测项展示，风险维度列取所属维度中文名）
    rule_rows = {row["rule_id"]: row for row in data_flow["rules"]}
    assert list(rule_rows) == ["TT5", "TT3"]  # 高风险检测项在前
    assert rule_rows["TT5"]["rule_name"] == "外部输入执行流"
    assert rule_rows["TT5"]["stats"] == {"high": 1, "medium": 0, "low": 0, "total": 1}
    assert rule_rows["TT5"]["max_severity_group"] == "high"
    assert "外部输入绝不经" in rule_rows["TT5"]["remediation"]
    assert rule_rows["TT3"]["max_severity_group"] == "low"
    assert rule_rows["TT3"]["stats"]["low"] == 1

    prompt = dimensions["prompt_injection"]
    assert prompt["max_severity_group"] == "medium"
    assert prompt["stats"]["total"] == 1


@pytest.mark.parametrize(
    ("score", "level", "label"),
    [
        (0, "safe", "安全"),
        (20, "safe", "安全"),
        (21, "low", "低风险"),
        (50, "low", "低风险"),
        (51, "medium", "中风险"),
        (80, "medium", "中风险"),
        (81, "high", "高风险"),
        (100, "high", "高风险"),
    ],
)
def test_build_report_level_tiers(score, level, label):
    result = build_audit_report(_skill(), _audit([], score=score))

    assert result["level"] == level
    assert result["level_label"] == label


def test_build_report_score_fallbacks():
    """details 缺分数时回退 report.risk_assessment.score，再回退 skills.risk_score。"""
    audit = _audit([], score=None, skillspector_score=None)
    audit.details["skillspector_report"]["risk_assessment"]["score"] = 65
    assert build_audit_report(_skill(), audit)["score"] == 65

    audit = _audit([], score=None, skillspector_score=None)
    audit.details["skillspector_report"]["risk_assessment"].pop("score")
    assert build_audit_report(_skill(risk_score=42), audit)["score"] == 42


def test_build_report_unknown_score_uses_unknown_level():
    audit = _audit([], score=None, skillspector_score=None)
    audit.details["skillspector_report"]["risk_assessment"].pop("score")

    result = build_audit_report(_skill(risk_score=None), audit)

    assert result["score"] is None
    assert result["level"] == "unknown"
    assert result["level_label"] == "未检测"


def test_build_report_summary_mentions_counts_and_suggestion():
    issues = [
        _issue("P1", "Prompt Injection", "HIGH"),
        _issue("E1", "Data Exfiltration", "MEDIUM"),
    ]
    result = build_audit_report(_skill(), _audit(issues, score=90))

    assert result["level"] == "high"
    summary = result["summary"]
    assert "90/100" in summary
    assert "高风险" in summary
    assert "1 项高风险" in summary
    assert "1 项中风险" in summary
    assert "建议" in summary


def test_build_report_truncates_findings(monkeypatch):
    monkeypatch.setattr(audit_report_module, "MAX_FINDINGS", 2)
    issues = [_issue(f"P{i}", "Prompt Injection", "LOW") for i in range(1, 6)]

    result = build_audit_report(_skill(), _audit(issues))

    assert len(result["findings"]) == 2
    assert result["findings_truncated"] is True
    # 统计与分类仍按全量计算
    assert result["stats"]["total"] == 5


def test_build_report_handles_missing_optional_fields():
    """报告缺 location / code_snippet 时不报错；缺 remediation 时回退规则目录中文建议。"""
    issues = [_issue("P1", "Prompt Injection", "HIGH", remediation=None, code_snippet=None)]
    issues[0]["location"] = {}
    result = build_audit_report(_skill(), _audit(issues))

    finding = result["findings"][0]
    assert finding["code_snippet"] is None
    assert finding["location"] is None
    # SkillSpector 未给 remediation，但仍按规则目录输出中文建议
    assert finding["remediation"] == "删除或重写任何让代理忽略提示、覆盖安全规则或不信任未验证内容的文本"


def test_build_report_unknown_rule_has_no_english_text():
    """规则目录之外的检测项：描述走维度中文说明，不透出英文原文，也无英文建议。"""
    issues = [_issue("ZZ9", None, "LOW", explanation="mystery english text", remediation="fix it")]
    result = build_audit_report(_skill(), _audit(issues))

    finding = result["findings"][0]
    assert finding["rule_name"] is None
    assert finding["remediation"] is None
    assert finding["title"] == "危险代码语法：代码中出现危险执行调用（exec/eval/subprocess/os.system、动态 import/getattr、反序列化链），可直接执行任意代码或绕过静态检测"


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        ({"category": " mcp tool poisoning ", "id": "TP1"}, "mcp_tool_poisoning"),
        ({"category": "Anti-Refusal", "id": "AR1"}, "anti_refusal"),
        ({"category": "Output Handling", "id": "OH2"}, "output_handling"),
        ({"category": None, "id": "AST7"}, "dangerous_code"),
        ({"category": None, "id": "TT3"}, "data_flow"),
        ({"category": None, "id": "YR2"}, "yara_match"),
        ({"category": None, "id": "PE1"}, "privilege_escalation"),
        # 规则目录之外的规则按前缀兜底
        ({"category": None, "id": "SSRF2"}, "tool_misuse"),
        ({"category": None, "id": "DS4"}, "dangerous_code"),
        ({"category": None, "id": "BH2"}, "data_exfiltration"),
        ({"category": None, "id": "unknown"}, "dangerous_code"),
        ({"category": "Mystery", "id": "ZZ9"}, "dangerous_code"),
    ],
)
def test_resolve_dimension(issue, expected):
    assert resolve_dimension(issue) == expected


# ------------------------------------------------------- 分类体系（规则目录）


def test_rule_catalog_covers_four_categories_and_seventeen_dimensions():
    """四大类 -> 17 维度 -> 检测项 的层级关系与固定数量（5 / 2 / 9 / 1）。"""
    from src.api.services.skillspector_rules import CATEGORIES, DIMENSIONS, RULE_CATALOG

    assert [len(category.dimensions) for category in CATEGORIES] == [5, 2, 9, 1]
    assert len(DIMENSIONS) == 17
    # 17 个维度无重复且全部挂在四大类上
    dimension_keys = [dimension.key for dimension in DIMENSIONS]
    assert len(set(dimension_keys)) == 17
    grouped = [key for category in CATEGORIES for key in category.dimensions]
    assert sorted(grouped) == sorted(dimension_keys)
    # 每个检测项都映射到一个已定义的维度
    assert RULE_CATALOG
    assert {rule.dimension for rule in RULE_CATALOG} <= set(dimension_keys)
    # 检测项无重复，且中文名称与建议均已填充
    rule_ids = [rule.rule_id for rule in RULE_CATALOG]
    assert len(set(rule_ids)) == len(rule_ids)
    assert all(rule.name and rule.remediation for rule in RULE_CATALOG)


def test_rule_catalog_keeps_documented_dimension_assignments():
    """关键规则与维度的映射关系（对齐 RISK_MODEL_SUMMARY.md）。"""
    from src.api.services.skillspector_rules import RULE_DIMENSIONS

    assert RULE_DIMENSIONS["P6"] == "system_prompt_leakage"
    assert RULE_DIMENSIONS["AR1"] == "anti_refusal"
    assert RULE_DIMENSIONS["TT5"] == "data_flow"
    assert RULE_DIMENSIONS["LP3"] == "mcp_least_privilege"
    assert RULE_DIMENSIONS["SC8"] == "supply_chain"
    # 扩展规则并入标准维度（保持 17 维度体系）
    assert RULE_DIMENSIONS["AS3"] == "privilege_escalation"
    assert RULE_DIMENSIONS["SSRF1"] == "tool_misuse"
    assert RULE_DIMENSIONS["DS3"] == "dangerous_code"


# ------------------------------------------------------------------ route


def _patch_route(skill, audit):
    repo = MagicMock()
    repo.get_by_skill_id = AsyncMock(return_value=skill)
    service = MagicMock()
    service.audit_repo = MagicMock()
    service.audit_repo.get_latest_by_resource = AsyncMock(return_value=audit)
    return repo, service


@pytest.mark.asyncio
async def test_route_returns_report():
    issues = [_issue("AST2", None, "CRITICAL")]
    repo, service = _patch_route(_skill(), _audit(issues))
    with (
        patch("src.api.routes.skills.SkillRepository", return_value=repo),
        patch("src.api.routes.skills.SecurityService", return_value=service),
    ):
        result = await get_skill_audit_report(skill_id="github:openeuler/find-skills/find-skills", db=AsyncMock())

    assert result.has_report is True
    assert result.skill_name == "find-skills"
    assert result.findings[0].dimension == "危险代码语法"
    assert result.dimensions[0].key == "dangerous_code"
    assert result.dimensions[0].rule_ids == ["AST2"]
    service.audit_repo.get_latest_by_resource.assert_awaited_once_with("skill", "skill-uuid")


@pytest.mark.asyncio
async def test_route_returns_no_audit_payload():
    repo, service = _patch_route(_skill(), None)
    with (
        patch("src.api.routes.skills.SkillRepository", return_value=repo),
        patch("src.api.routes.skills.SecurityService", return_value=service),
    ):
        result = await get_skill_audit_report(skill_id="github:openeuler/find-skills/find-skills", db=AsyncMock())

    assert result.has_report is False
    assert result.reason == "no_audit"


@pytest.mark.asyncio
async def test_route_unknown_skill_404():
    repo, service = _patch_route(None, None)
    with (
        patch("src.api.routes.skills.SkillRepository", return_value=repo),
        patch("src.api.routes.skills.SecurityService", return_value=service),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_skill_audit_report(skill_id="github:ghost/ghost/ghost", db=AsyncMock())

    assert exc_info.value.status_code == 404
    service.audit_repo.get_latest_by_resource.assert_not_awaited()


def test_endpoint_http_roundtrip():
    """走 HTTP 全链路：验证路由未被 catch-all 吞掉 + {code,msg,data} 包装 + 响应模型序列化。"""
    from fastapi.testclient import TestClient

    from src.api.main import app
    from src.core.database import get_db

    repo, service = _patch_route(_skill(), _audit([_issue("P1", "Prompt Injection", "HIGH")]))

    async def _fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = _fake_db
    try:
        with (
            patch("src.api.routes.skills.SkillRepository", return_value=repo),
            patch("src.api.routes.skills.SecurityService", return_value=service),
        ):
            client = TestClient(app)
            # 前端以 encodeURIComponent(skill_id) 拼 URL（: 与 / 均被转义）
            response = client.get(
                "/api/v1/skills/github%3Aopeneuler%2Ffind-skills%2Ffind-skills/audit-report"
            )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["msg"] == "ok"
    assert body["data"]["has_report"] is True
    assert body["data"]["skill_name"] == "find-skills"
    assert body["data"]["stats"]["high"] == 1
    assert body["data"]["rule_count"] == len(RULE_CATALOG)
    assert body["data"]["categories"][0]["dimension_count"] == 5
    # 分类 -> 维度 -> 检测项 全链路透出（响应模型含 dimensions）
    assert body["data"]["categories"][0]["dimensions"][0] == {"key": "prompt_injection", "name": "提示注入"}
    assert body["data"]["dimensions"][0]["key"] == "prompt_injection"
    assert body["data"]["dimensions"][0]["rule_ids"] == ["P1"]
    assert body["data"]["findings"][0]["rule_name"] == "覆盖指令"