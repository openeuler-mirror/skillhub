"""单条审计复核流程的纯逻辑测试（分批游标与上下文元数据）。"""

from src.core.config import AuditLLMReviewConfig
from src.security.audit_review.constants import PROMPT_VERSION
from src.security.audit_review.review import build_partial_meta


class TestPartialMeta:
    def test_keeps_cursor_and_false_positive_indices(self):
        config = AuditLLMReviewConfig(api_key="test")
        meta = build_partial_meta(
            config,
            issues_count=10434,
            reviewed_count=200,
            false_positive_indices={3, 1},
            false_positive_reasons=[{"index": 1, "reason": "doc example"}],
            low_confidence_indices=set(),
            low_confidence_reasons=[],
            started_at="2026-10-29T02:00:00",
        )
        assert meta["status"] == "partial"
        assert meta["findings_total"] == 10434
        assert meta["reviewed_count"] == 200
        assert meta["false_positive_indices"] == [1, 3]
        assert meta["false_positive_count"] == 2
        assert meta["started_at"] == "2026-10-29T02:00:00"
        assert meta["attempts"] == 0
        assert meta["prompt_version"] == PROMPT_VERSION
        assert meta["min_confidence"] == 0.6
        # 元数据时间统一带窗口时区（默认北京时间）
        assert meta["updated_at"].endswith("+08:00")

    def test_metadata_time_uses_configured_timezone(self):
        config = AuditLLMReviewConfig(api_key="test", timezone="UTC")
        meta = build_partial_meta(
            config,
            issues_count=1,
            reviewed_count=1,
            false_positive_indices=set(),
            false_positive_reasons=[],
            low_confidence_indices=set(),
            low_confidence_reasons=[],
            started_at=None,
        )
        assert meta["updated_at"].endswith("+00:00")

    def test_keeps_low_confidence_indices(self):
        config = AuditLLMReviewConfig(api_key="test")
        meta = build_partial_meta(
            config,
            issues_count=10,
            reviewed_count=4,
            false_positive_indices=set(),
            false_positive_reasons=[],
            low_confidence_indices={3},
            low_confidence_reasons=[{"index": 3, "reason": "unsure", "confidence": 0.3}],
            started_at=None,
        )
        assert meta["low_confidence_indices"] == [3]
        assert meta["low_confidence_count"] == 1
        assert "confirmed_findings" not in meta


class TestPartialMetaConfidence:
    def test_keeps_false_positive_confidence(self):
        config = AuditLLMReviewConfig(api_key="test", context_mode="window")
        meta = build_partial_meta(
            config,
            issues_count=3,
            reviewed_count=1,
            false_positive_indices={1},
            false_positive_reasons=[{"index": 1, "reason": "doc", "confidence": 0.9}],
            low_confidence_indices=set(),
            low_confidence_reasons=[],
            started_at=None,
        )

        assert meta["false_positive_reasons"][0]["confidence"] == 0.9
        assert meta["min_false_positive_confidence"] == 0.85
        assert meta["min_false_positive_confidence_high"] == 0.9
