"""LLM 复核调度器测试：分批续跑、凭证回退与窗口校验。"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.core.config import AuditLLMReviewConfig
from src.jobs.audit_llm_review import (
    AuditLLMReviewScheduler,
    ReviewDeadlineError,
)
from src.security.audit_review.queue import _RateLimiter, parse_hhmm


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


class TestMultiPass:
    @pytest.mark.asyncio
    async def test_idle_queue_repeats_same_audit_in_one_run(self, monkeypatch, tmp_path):
        scheduler = AuditLLMReviewScheduler(
            AuditLLMReviewConfig(
                api_key="test",
                max_findings_per_audit=200,
                result_dir=str(tmp_path),
            )
        )
        row = {"id": "audit-1", "audited_at": datetime.now(), "details": make_details()}
        fetch_calls: list[object] = []

        async def fake_fetch(limit, after=None):
            fetch_calls.append(after)
            # 第 1、3 次取到同一审计；第 2、4 次为空触发换轮；第 5 次确认无事可做
            if len(fetch_calls) in (1, 3):
                return [row]
            return []

        async def fake_review(client, limiter, current, deadline=None):
            return "partial"

        monkeypatch.setattr(scheduler, "_fetch_pending", fake_fetch)
        monkeypatch.setattr(scheduler, "_review_one", fake_review)

        stats = await scheduler.run_once(deadline=scheduler._now() + timedelta(seconds=60))

        # 队列空闲时同一审计连续推进了两轮，而不是等下一个晚上
        assert stats["partial"] == 2
        assert stats["stopped"] == "no_work"
        assert len(fetch_calls) == 5
        # keyset 游标是 (community_priority, risk_rank, issue_count, audited_at, id) 五元组
        assert fetch_calls[0] is None
        assert isinstance(fetch_calls[1], tuple)
        assert len(fetch_calls[1]) == 5


class TestFetchPendingOrder:
    @pytest.mark.asyncio
    async def test_orders_by_community_then_risk_then_issue_count(self, monkeypatch):
        import src.jobs.audit_llm_review as module

        captured: dict[str, str] = {}

        class FakeResult:
            def __iter__(self):
                return iter([])

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, stmt):
                captured["sql"] = str(stmt)
                return FakeResult()

        monkeypatch.setattr(module, "AsyncSessionLocal", lambda: FakeSession())
        scheduler = AuditLLMReviewScheduler(AuditLLMReviewConfig(api_key="test"))
        assert await scheduler._fetch_pending(10) == []

        sql = captured["sql"]
        assert "jsonb_array_length" in sql
        order_clause = sql[sql.index("ORDER BY") :]
        assert "skills.platform" in order_clause
        assert order_clause.index("skills.platform") < order_clause.index(
            "security_audits.risk_level"
        )
        assert order_clause.index("security_audits.risk_level") < order_clause.index(
            "jsonb_array_length"
        )
        assert order_clause.index("jsonb_array_length") < order_clause.index(
            "security_audits.audited_at"
        )


class TestDeadline:
    @pytest.mark.asyncio
    async def test_review_one_stops_before_next_chunk(self):
        scheduler = AuditLLMReviewScheduler(AuditLLMReviewConfig(api_key="test"))
        details = make_details()
        row = {
            "id": "audit-id",
            "details": details,
            "risk_signals": details["risk_signals"],
            "risk_level": "medium",
        }
        with pytest.raises(ReviewDeadlineError):
            await scheduler._review_one(
                None,
                _RateLimiter(rpm=0, tpm=0),
                row,
                deadline=scheduler._now() - timedelta(days=1),
            )


class TestModelFallback:
    def test_explicit_values_win(self, monkeypatch):
        import src.jobs.audit_llm_review as module

        monkeypatch.setattr(
            module,
            "get_settings",
            lambda: SimpleNamespace(
                model=SimpleNamespace(
                    name="provider-model",
                    base_url="https://provider.example/v1",
                    api_key="provider-key",
                )
            ),
        )
        scheduler = AuditLLMReviewScheduler(
            AuditLLMReviewConfig(
                model="explicit-model",
                base_url="https://explicit.example/v1",
                api_key="explicit-key",
            )
        )
        assert scheduler._config.model == "explicit-model"
        assert scheduler._config.base_url == "https://explicit.example/v1"
        assert scheduler._api_key == "explicit-key"

    def test_falls_back_to_active_provider(self, monkeypatch):
        import src.jobs.audit_llm_review as module

        monkeypatch.setattr(
            module,
            "get_settings",
            lambda: SimpleNamespace(
                model=SimpleNamespace(
                    name="provider-model",
                    base_url="https://provider.example/v1",
                    api_key="provider-key",
                )
            ),
        )
        scheduler = AuditLLMReviewScheduler(AuditLLMReviewConfig())
        assert scheduler._config.model == "provider-model"
        assert scheduler._config.base_url == "https://provider.example/v1"
        assert scheduler._api_key == "provider-key"

    def test_no_provider_config_keeps_empty(self, monkeypatch):
        import src.jobs.audit_llm_review as module

        monkeypatch.setattr(
            module,
            "get_settings",
            lambda: SimpleNamespace(model=SimpleNamespace()),
        )
        scheduler = AuditLLMReviewScheduler(AuditLLMReviewConfig())
        assert scheduler._config.model == ""
        assert scheduler._config.base_url == ""
        assert scheduler._api_key == ""


class TestConfig:
    def test_code_defaults(self):
        config = AuditLLMReviewConfig()
        assert config.enabled is False
        assert config.start_time == "01:00"
        assert config.end_time == "08:00"
        assert config.timezone == "Asia/Shanghai"
        assert config.model == ""
        assert config.base_url == ""
        assert config.max_findings_per_audit == 200

    def test_config_yaml_section_parsed(self):
        from src.core.config import Settings

        settings = Settings.from_yaml("config.yaml")
        assert settings.audit_llm_review.enabled is True
        assert settings.audit_llm_review.start_time == "01:00"
        assert settings.audit_llm_review.end_time == "08:00"
        assert settings.audit_llm_review.timezone == "Asia/Shanghai"
        # 留空表示复用 model.providers 当前 provider
        assert settings.audit_llm_review.model == ""
        assert settings.audit_llm_review.base_url == ""

    def test_env_override_keeps_hhmm_string(self, monkeypatch):
        from src.core.config import Settings

        monkeypatch.setenv("AUDIT_LLM_REVIEW__START_TIME", "2:00")
        monkeypatch.setenv("AUDIT_LLM_REVIEW__END_TIME", "8:00")
        settings = Settings.from_yaml("config.yaml")
        assert settings.audit_llm_review.start_time == "2:00"
        assert settings.audit_llm_review.end_time == "8:00"
        assert parse_hhmm(settings.audit_llm_review.start_time) == (2, 0)


class TestSchedulerWindowValidation:
    def test_scheduler_rejects_reversed_window(self):
        scheduler = AuditLLMReviewScheduler(
            AuditLLMReviewConfig(start_time="08:00", end_time="02:00")
        )
        with pytest.raises(ValueError):
            scheduler._check_window_args()


class TestWindowTimezone:
    def test_window_clock_ignores_container_tz(self, monkeypatch):
        # 容器 TZ 为 UTC 时，窗口仍按配置时区（默认北京时间）解释
        monkeypatch.setenv("TZ", "UTC")
        scheduler = AuditLLMReviewScheduler(AuditLLMReviewConfig(api_key="test"))
        assert scheduler._now().utcoffset() == timedelta(hours=8)

    def test_window_clock_follows_configured_timezone(self):
        scheduler = AuditLLMReviewScheduler(
            AuditLLMReviewConfig(api_key="test", timezone="UTC")
        )
        assert scheduler._now().utcoffset() == timedelta(0)
