"""LLM 复核结论应用与误报删除门槛测试。"""

from src.security.audit_review.analysis import compute_skillspector_score
from src.security.audit_review.apply import _removal_kind, apply_review
from src.security.audit_review.constants import PROMPT_VERSION


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


class TestApplyReview:
    def test_removes_false_positive_and_rescores(self):
        details = make_details()
        application = apply_review(
            details=details,
            risk_signals=details["risk_signals"],
            current_risk_level="medium",
            verdicts={
                0: {"is_vulnerability": False, "reason": "documented install step"},
                1: {"is_vulnerability": True, "reason": "real risk"},
            },
            model="test-model",
            reviewed_at="2026-10-29T03:00:00",
            skipped=0,
        )

        issues = application.details["skillspector_report"]["issues"]
        assert len(issues) == 2
        assert issues[0]["id"] == "TT3"
        # 确认项不写入额外字段，保持原始 issue 结构
        assert "llm_review" not in issues[0]
        # 剔除 HIGH(25*0.8=20) 后，原生公式重算：10*0.5 + 5*1.0 = 10
        assert application.score == 10
        assert application.risk_level == "low"
        assert application.reviewed == 2
        assert application.confirmed == 1
        assert application.false_positives == 1
        assert len(application.risk_signals) == 2
        assert all(signal["id"] != "PE2" for signal in application.risk_signals)
        assert application.details["skillspector_score"] == 10
        assert application.details["recommendation"] == "SAFE"
        assert application.details["risk_level"] == "low"
        assert application.details["skillspector_report"]["risk_assessment"]["score"] == 10
        review_meta = application.details["llm_review"]
        assert review_meta["status"] == "completed"
        assert review_meta["applied"] is True
        assert review_meta["all_removed"] is False
        assert review_meta["findings_false_positive"] == 1
        assert review_meta["findings_low_confidence"] == 0
        assert review_meta["prompt_version"] == PROMPT_VERSION
        assert review_meta["false_positive_findings"][0]["id"] == "PE2"
        # 原始 details 不应被修改
        assert len(details["skillspector_report"]["issues"]) == 3

    def test_unjudged_findings_are_kept(self):
        details = make_details()
        application = apply_review(
            details=details,
            risk_signals=details["risk_signals"],
            current_risk_level="medium",
            verdicts={0: {"is_vulnerability": False, "reason": "doc"}},
            model="test-model",
            reviewed_at="2026-10-29T03:00:00",
            skipped=0,
        )
        assert len(application.details["skillspector_report"]["issues"]) == 2
        assert application.reviewed == 1
        assert application.confirmed == 0
        assert application.score == 10

    def test_empty_verdicts_keep_everything(self):
        details = make_details()
        application = apply_review(
            details=details,
            risk_signals=details["risk_signals"],
            current_risk_level="medium",
            verdicts={},
            model="test-model",
            reviewed_at="2026-10-29T03:00:00",
            skipped=3,
        )
        assert len(application.details["skillspector_report"]["issues"]) == 3
        # 没有命中被删除时，原生公式重算结果与原分完全一致
        assert application.score == 30
        assert application.risk_level == "medium"
        assert len(application.risk_signals) == 3
        assert application.details["llm_review"]["findings_skipped"] == 3

    def test_all_false_positive_applies_zero(self):
        details = make_details()
        verdicts = {index: {"is_vulnerability": False, "reason": "doc"} for index in range(3)}
        application = apply_review(
            details=details,
            risk_signals=details["risk_signals"],
            current_risk_level="medium",
            verdicts=verdicts,
            model="test-model",
            reviewed_at="2026-10-29T03:00:00",
            skipped=0,
            reviewed_total=3,
            started_at="2026-10-28T02:00:00",
        )
        # 不再保留原结果，直接应用全删结论
        assert application.details["skillspector_report"]["issues"] == []
        assert application.score == 0
        assert application.risk_level == "low"
        assert application.details["skillspector_score"] == 0
        assert application.details["recommendation"] == "SAFE"
        review_meta = application.details["llm_review"]
        assert review_meta["status"] == "completed"
        assert review_meta["all_removed"] is True
        assert review_meta["applied"] is True
        assert review_meta["findings_reviewed"] == 3
        assert review_meta["findings_false_positive"] == 3
        assert review_meta["score_before"] == 30
        assert review_meta["score_after"] == 0
        assert review_meta["risk_level_after"] == "low"
        assert review_meta["started_at"] == "2026-10-28T02:00:00"
        # 原始 details 不应被修改
        assert len(details["skillspector_report"]["issues"]) == 3

    def test_partial_false_positive_still_applies(self):
        details = make_details()
        application = apply_review(
            details=details,
            risk_signals=details["risk_signals"],
            current_risk_level="medium",
            verdicts={
                0: {"is_vulnerability": False, "reason": "doc"},
                1: {"is_vulnerability": False, "reason": "doc"},
            },
            model="test-model",
            reviewed_at="2026-10-29T03:00:00",
            skipped=0,
        )
        assert len(application.details["skillspector_report"]["issues"]) == 1
        # 只保留 SC2 LOW 1.0 -> 5
        assert application.score == 5
        assert application.risk_level == "low"

    def test_low_confidence_true_finding_is_removed(self):
        """单向门槛与上游一致：确认为真但 confidence<阈值同样丢弃。"""
        details = make_details()
        application = apply_review(
            details=details,
            risk_signals=details["risk_signals"],
            current_risk_level="medium",
            verdicts={
                0: {
                    "is_vulnerability": True,
                    "confidence": 0.4,
                    "reason": "uncertain",
                }
            },
            model="test-model",
            reviewed_at="2026-10-29T03:00:00",
            skipped=0,
            min_confidence=0.6,
        )
        review_meta = application.details["llm_review"]
        assert application.false_positives == 0
        assert application.low_confidence == 1
        assert review_meta["findings_low_confidence"] == 1
        assert review_meta["low_confidence_findings"][0]["id"] == "PE2"
        assert review_meta["min_confidence"] == 0.6
        # 低于阈值的 HIGH 命中被丢弃，剩余 10*0.5 + 5*1.0 = 10
        assert application.score == 10

    def test_min_confidence_zero_keeps_low_confidence_findings(self):
        details = make_details()
        application = apply_review(
            details=details,
            risk_signals=details["risk_signals"],
            current_risk_level="medium",
            verdicts={
                0: {
                    "is_vulnerability": True,
                    "confidence": 0.1,
                    "reason": "uncertain",
                }
            },
            model="test-model",
            reviewed_at="2026-10-29T03:00:00",
            skipped=0,
            min_confidence=0,
        )
        assert application.low_confidence == 0
        assert application.score == 30
        assert len(application.details["skillspector_report"]["issues"]) == 3

    def test_missing_original_score_uses_native_formula(self):
        details = make_details()
        details["skillspector_score"] = None
        details["skillspector_report"]["risk_assessment"].pop("score")
        application = apply_review(
            details=details,
            risk_signals=details["risk_signals"],
            current_risk_level="unknown",
            verdicts={},
            model="test-model",
            reviewed_at="2026-10-29T03:00:00",
            skipped=0,
        )
        # (25*0.8 + 10*0.5 + 5*1.0) = 30
        assert application.score == 30
        assert application.risk_level == "medium"

    def test_reviewed_total_marks_all_findings_reviewed(self):
        details = make_details()
        application = apply_review(
            details=details,
            risk_signals=details["risk_signals"],
            current_risk_level="medium",
            verdicts={1: {"is_vulnerability": False, "reason": "doc"}},
            model="test-model",
            reviewed_at="2026-10-29T03:00:00",
            skipped=0,
            reviewed_total=3,
            started_at="2026-10-28T02:00:00",
        )
        review_meta = application.details["llm_review"]
        assert review_meta["findings_reviewed"] == 3
        assert review_meta["findings_confirmed"] == 2
        assert review_meta["findings_false_positive"] == 1
        assert review_meta["started_at"] == "2026-10-28T02:00:00"
        # 剔除 MEDIUM(10*0.5=5) 后：20 + 5 = 25
        assert application.score == 25


class TestFalsePositiveThreshold:
    def test_false_positive_below_threshold_is_kept(self):
        details = make_details()
        application = apply_review(
            details=details,
            risk_signals=details["risk_signals"],
            current_risk_level="medium",
            verdicts={0: {"is_vulnerability": False, "confidence": 0.7, "reason": "doc"}},
            model="test-model",
            reviewed_at="2026-10-31T03:00:00",
            skipped=0,
            min_false_positive_confidence=0.85,
        )

        assert len(application.details["skillspector_report"]["issues"]) == 3
        assert application.false_positives == 0
        meta = application.details["llm_review"]
        assert meta["findings_false_positive_below_threshold"] == 1
        assert meta["min_false_positive_confidence"] == 0.85
        # 命中被保留时，对应的静态风险信号也必须保留
        assert [signal["id"] for signal in application.risk_signals] == [
            "PE2",
            "TT3",
            "SC2",
        ]

    def test_false_positive_above_threshold_is_removed_and_confidence_persisted(self):
        details = make_details()
        application = apply_review(
            details=details,
            risk_signals=details["risk_signals"],
            current_risk_level="medium",
            verdicts={0: {"is_vulnerability": False, "confidence": 0.95, "reason": "doc"}},
            model="test-model",
            reviewed_at="2026-10-31T03:00:00",
            skipped=0,
            min_false_positive_confidence=0.85,
            min_false_positive_confidence_high=0.9,
        )

        assert application.false_positives == 1
        meta = application.details["llm_review"]
        detail = meta["false_positive_findings"][0]
        assert detail["confidence"] == 0.95
        assert detail["id"] == "PE2"

    def test_high_severity_uses_stricter_threshold(self):
        details = make_details()
        application = apply_review(
            details=details,
            risk_signals=details["risk_signals"],
            current_risk_level="medium",
            verdicts={0: {"is_vulnerability": False, "confidence": 0.87, "reason": "doc"}},
            model="test-model",
            reviewed_at="2026-10-31T03:00:00",
            skipped=0,
            min_false_positive_confidence=0.85,
            min_false_positive_confidence_high=0.9,
        )

        # PE2 是 HIGH，0.87 低于 0.9 的 HIGH 门槛，应保留
        assert application.false_positives == 0
        assert len(application.details["skillspector_report"]["issues"]) == 3

    def test_missing_confidence_keeps_false_positive(self):
        details = make_details()
        application = apply_review(
            details=details,
            risk_signals=details["risk_signals"],
            current_risk_level="medium",
            verdicts={0: {"is_vulnerability": False, "reason": "doc"}},
            model="test-model",
            reviewed_at="2026-10-31T03:00:00",
            skipped=0,
            min_false_positive_confidence=0.85,
        )

        assert application.false_positives == 0
        assert application.details["llm_review"]["findings_false_positive_below_threshold"] == 1

    def test_threshold_zero_disables_gate(self):
        details = make_details()
        application = apply_review(
            details=details,
            risk_signals=details["risk_signals"],
            current_risk_level="medium",
            verdicts={0: {"is_vulnerability": False, "reason": "doc"}},
            model="test-model",
            reviewed_at="2026-10-31T03:00:00",
            skipped=0,
            min_false_positive_confidence=0,
        )

        assert application.false_positives == 1

    def test_removal_kind_threshold(self):
        assert _removal_kind({"is_vulnerability": False, "confidence": 0.7}, 0.6, 0.85) is None
        assert (
            _removal_kind({"is_vulnerability": False, "confidence": 0.9}, 0.6, 0.85)
            == "false_positive"
        )
        assert _removal_kind({"is_vulnerability": False}, 0.6, 0.85) is None


class TestNativeScoring:
    def _issue(self, rule_id: str, severity: str, confidence: float) -> dict:
        return {"id": rule_id, "severity": severity, "confidence": confidence}

    def test_per_rule_diminishing_returns(self):
        # 同一规则 4 次命中：25*1 + 25*0.5 + 25*0.25 + 0 = 43.75 -> 43
        issues = [self._issue("AST1", "HIGH", 1.0) for _ in range(4)]
        assert compute_skillspector_score(issues, False) == 43

    def test_executable_script_multiplier(self):
        issues = [self._issue("PE2", "HIGH", 0.8)]
        assert compute_skillspector_score(issues, False) == 20
        assert compute_skillspector_score(issues, True) == 26

    def test_missing_confidence_defaults_to_half(self):
        issues = [{"id": "TT3", "severity": "MEDIUM"}]
        assert compute_skillspector_score(issues, False) == 5

    def test_zero_confidence_is_ignored(self):
        issues = [self._issue("TT3", "MEDIUM", 0.0)]
        assert compute_skillspector_score(issues, False) == 0

    def test_score_capped_at_100(self):
        issues = [self._issue(f"CR{i}", "CRITICAL", 1.0) for i in range(10)]
        assert compute_skillspector_score(issues, False) == 100
