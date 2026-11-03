"""LLM 复核提示词构造与模型响应解析测试。"""

import pytest

from src.security.audit_review.constants import DEFAULT_VERDICT_CONFIDENCE
from src.security.audit_review.prompt import (
    LLMReviewError,
    _extract_json_object,
    _match_verdicts,
    _normalise_verdicts,
    build_user_prompt,
)


def make_details() -> dict:
    # 原生评分：HIGH(25)*0.8 + MEDIUM(10)*0.5 + LOW(5)*1.0 = 30
    return {
        "skillspector_score": 30,
        "recommendation": "CAUTION",
        "risk_level": "medium",
        "risk_signals": [
            {"id": "PE2", "data": {"location": {"file": "a.md", "start_line": 10}}},
            {"id": "TT3", "data": {"location": {"file": "a.md", "start_line": 20}}},
            {"id": "SC2", "data": {"location": {"file": "b.md", "start_line": 5}}},
        ],
        "skillspector_report": {
            "issues": [
                {
                    "id": "PE2",
                    "severity": "HIGH",
                    "confidence": 0.8,
                    "location": {"file": "a.md", "start_line": 10},
                },
                {
                    "id": "TT3",
                    "severity": "MEDIUM",
                    "confidence": 0.5,
                    "location": {"file": "a.md", "start_line": 20},
                },
                {
                    "id": "SC2",
                    "severity": "LOW",
                    "confidence": 1.0,
                    "location": {"file": "b.md", "start_line": 5},
                },
            ],
            "risk_assessment": {
                "score": 30,
                "severity": "MEDIUM",
                "recommendation": "CAUTION",
            },
            "metadata": {"has_executable_scripts": False, "scanned_at": "2026-10-28T00:00:00"},
        },
    }


class TestJsonParsing:
    def test_plain_json(self):
        assert _extract_json_object('{"findings": []}') == {"findings": []}

    def test_fenced_with_prose(self):
        text = 'Here is the result:\n```json\n{"findings": [1]}\n```\nDone.'
        assert _extract_json_object(text) == {"findings": [1]}

    def test_invalid_raises(self):
        with pytest.raises(LLMReviewError):
            _extract_json_object("not json at all")


class TestNormaliseVerdicts:
    def test_aliases_and_bool_coercion(self):
        payload = {
            "findings": [
                {
                    "id": "[PE2] Sudo",
                    "index": "1",
                    "is_true_positive": "false",
                    "explanation": "documented example",
                },
                {
                    "pattern_id": "TT3",
                    "line": "20",
                    "vulnerable": "yes",
                    "reason": "real risk",
                },
            ]
        }
        verdicts = _normalise_verdicts(payload)
        assert verdicts == [
            {
                "index": 1,
                "rule_id": "PE2",
                "start_line": None,
                "is_vulnerability": False,
                "confidence": DEFAULT_VERDICT_CONFIDENCE,
                "reason": "documented example",
            },
            {
                "index": None,
                "rule_id": "TT3",
                "start_line": 20,
                "is_vulnerability": True,
                "confidence": DEFAULT_VERDICT_CONFIDENCE,
                "reason": "real risk",
            },
        ]

    def test_unrecognised_bool_is_dropped(self):
        assert _normalise_verdicts({"findings": [{"id": "PE2", "is_vulnerability": "high"}]}) == []

    def test_top_level_single_verdict(self):
        assert (
            _normalise_verdicts({"id": "PE2", "is_vulnerability": False})[0]["is_vulnerability"]
            is False
        )

    def test_confidence_variants(self):
        verdicts = _normalise_verdicts(
            {
                "findings": [
                    {"index": 1, "is_vulnerability": True, "confidence": "85%"},
                    {"index": 2, "is_vulnerability": True, "confidence": 1.5},
                    {"index": 3, "is_vulnerability": True},
                ]
            }
        )
        assert verdicts[0]["confidence"] == pytest.approx(0.85)
        # 越界置信度被 clamp 到 1.0
        assert verdicts[1]["confidence"] == 1.0
        # 缺省置信度回退默认值
        assert verdicts[2]["confidence"] == DEFAULT_VERDICT_CONFIDENCE


class TestPrompt:
    def test_binary_snippet_is_not_sent_to_model(self):
        issues = [
            {
                "id": "TM1",
                "severity": "HIGH",
                "location": {"file": "logo.png", "start_line": 1},
                "code_snippet": "\x00\x01\x02\x03" * 50,
            }
        ]
        prompt = build_user_prompt(
            skill_name="demo",
            skill_description="",
            skill_content="",
            file_paths=[],
            issues=issues,
            chunk=[0],
            total_issues=1,
            skipped=0,
        )
        assert "(binary or non-text content omitted)" in prompt
        assert "\x00" not in prompt

    def test_prompt_lists_one_finding_per_index(self):
        issues = make_details()["skillspector_report"]["issues"]
        prompt = build_user_prompt(
            skill_name="demo",
            skill_description="desc",
            skill_content="content",
            file_paths=["SKILL.md"],
            issues=issues,
            chunk=[0, 1],
            total_issues=3,
            skipped=1,
        )
        assert "### Finding 1" in prompt
        assert "### Finding 2" in prompt
        assert "### Finding 3" not in prompt
        assert "1 other findings are not included" in prompt
        assert '"index":1' in prompt


class TestMatchVerdicts:
    def test_match_by_index(self):
        issues = make_details()["skillspector_report"]["issues"]
        matched = _match_verdicts(
            [{"index": 1, "is_vulnerability": False, "reason": "doc"}],
            [2, 0],
            issues,
        )
        assert matched[2]["is_vulnerability"] is False
        assert matched[2]["reason"] == "doc"
        assert matched[2]["confidence"] is None

    def test_match_by_rule_and_line(self):
        issues = make_details()["skillspector_report"]["issues"]
        matched = _match_verdicts(
            [
                {
                    "index": None,
                    "rule_id": "TT3",
                    "start_line": 20,
                    "is_vulnerability": True,
                    "reason": "real",
                }
            ],
            [0, 1],
            issues,
        )
        assert matched[1]["is_vulnerability"] is True
        assert matched[1]["reason"] == "real"

    def test_unmatched_verdict_ignored(self):
        issues = make_details()["skillspector_report"]["issues"]
        assert (
            _match_verdicts(
                [{"index": 9, "rule_id": "ZZ9", "is_vulnerability": False}],
                [0, 1],
                issues,
            )
            == {}
        )


class TestContextPrompt:
    def test_prompt_includes_file_content_and_untrusted_warning(self):
        issues = make_details()["skillspector_report"]["issues"]
        prompt = build_user_prompt(
            skill_name="demo",
            skill_description="",
            skill_content="",
            file_paths=["a.md"],
            issues=issues,
            chunk=[0],
            total_issues=3,
            skipped=2,
            file_contexts={"a.md": "  10| line 10\n  11| line 11"},
        )

        assert "## File contents" in prompt
        assert "untrusted data" in prompt
        assert '<file path="a.md">' in prompt
        assert "10| line 10" in prompt
