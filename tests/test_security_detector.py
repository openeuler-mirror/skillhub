from unittest.mock import AsyncMock, MagicMock, patch

from src.security.detector import (
    MAX_REPORT_FETCH_ATTEMPTS,
    _log_skillspector_final_result,
    _log_collector_event,
    _record_unavailable_report_attempt,
    _sanitize_json_value,
)


def test_sanitize_json_value_replaces_nested_nul_characters():
    value = {
        "plain": "unchanged",
        "nul\x00key": "value\x00with\x00nul",
        "nested": ["item\x00", {"tuple": ("a\x00b", 1, None)}],
    }

    assert _sanitize_json_value(value) == {
        "plain": "unchanged",
        "nul\\0key": "value\\0with\\0nul",
        "nested": ["item\\0", {"tuple": ["a\\0b", 1, None]}],
    }


def test_unavailable_report_is_finalized_after_retry_limit():
    details = {"skillspector_async": True}

    for attempt in range(1, MAX_REPORT_FETCH_ATTEMPTS + 1):
        details, exhausted = _record_unavailable_report_attempt(details, "FAILURE")
        assert details["skillspector_report_fetch_attempts"] == attempt
        assert exhausted is (attempt == MAX_REPORT_FETCH_ATTEMPTS)

    assert details["skillspector_collected"] is True
    assert details["skillspector_status"] == "FAILURE"
    assert details["skillspector_error"] == "report_unavailable"


def test_unavailable_report_attempt_recovers_from_invalid_counter():
    details, exhausted = _record_unavailable_report_attempt(
        {"skillspector_report_fetch_attempts": "invalid"},
        "UNSTABLE",
    )

    assert exhausted is False
    assert details["skillspector_report_fetch_attempts"] == 1
    assert details["skillspector_status"] == "UNSTABLE"
    assert "skillspector_collected" not in details


def test_missing_jenkins_artifact_is_finalized_immediately():
    details, exhausted = _record_unavailable_report_attempt(
        {"skillspector_async": True},
        "FAILURE",
        terminal=True,
    )

    assert exhausted is True
    assert details["skillspector_collected"] is True
    assert details["skillspector_report_fetch_attempts"] == 1
    assert details["skillspector_error"] == "report_unavailable"


def test_final_result_log_contains_report():
    with patch("src.security.detector.collector_logger.info") as log_info:
        _log_skillspector_final_result(
            audit_id="audit-1",
            build_number=123,
            status="SUCCESS",
            outcome="report_collected",
            risk_level="low",
            score=8,
            signal_count=1,
        )

    message = log_info.call_args.args[1]
    assert '"event":"skillspector_final_result"' in message
    assert '"build_number":123' in message
    assert '"risk_level":"low"' in message
    assert '"score":8' in message


def test_missing_jenkins_build_is_terminal():
    response = MagicMock(status_code=404)
    client_context = MagicMock()
    client_context.__enter__.return_value.get.return_value = response

    from src.security.detector import SkillspectorClient

    client = SkillspectorClient("http://jenkins", "admin", "token")
    with patch("src.security.detector.httpx.Client", return_value=client_context):
        assert client.get_build_status(14194) == "NOT_FOUND"


def test_collector_event_log_contains_decision():
    with patch("src.security.detector.collector_logger.info") as log_info:
        _log_collector_event(
            "item_deferred",
            audit_id="audit-1",
            build_number=456,
            jenkins_status="BUILDING",
            action="retry_next_poll",
        )

    message = log_info.call_args.args[1]
    assert '"event":"item_deferred"' in message
    assert '"build_number":456' in message
    assert '"action":"retry_next_poll"' in message


def test_run_scan_uses_report_even_when_build_not_success():
    """Critical findings make the Jenkins build exit non-zero, but report.json
    still exists.  The report artifact must be treated as the source of truth."""
    from src.security.detector import SkillspectorClient

    client = SkillspectorClient("http://jenkins", "admin", "token")
    report = {"risk_assessment": {"severity": "CRITICAL", "score": 95}}
    report_md = "# Security Report"

    with (
        patch.object(client, "trigger_scan", return_value=42),
        patch.object(client, "wait_for_build", return_value="FAILURE"),
        patch.object(client, "fetch_report", return_value=report) as mock_fetch,
        patch.object(client, "fetch_report_md", return_value=report_md) as mock_fetch_md,
    ):
        result, md = client.run_scan("https://gitcode.com/openeuler/foo")

    mock_fetch.assert_called_once_with(42)
    mock_fetch_md.assert_called_once_with(42)
    assert result == report
    assert md == report_md


def test_run_scan_errors_when_report_unavailable():
    from src.security.detector import SkillspectorClient

    client = SkillspectorClient("http://jenkins", "admin", "token")

    with (
        patch.object(client, "trigger_scan", return_value=42),
        patch.object(client, "wait_for_build", return_value="SUCCESS"),
        patch.object(client, "fetch_report", return_value=None),
    ):
        result, _ = client.run_scan("https://gitcode.com/openeuler/foo")

    assert "error" in result


def _detector_with_client(client):
    from src.security.detector import SecurityDetector

    detector = SecurityDetector()
    detector._skillspector_client = client
    return detector


def _await(coro):
    import asyncio

    return asyncio.run(coro)


def test_get_external_result_pending():
    from src.security.detector import SkillspectorClient

    client = SkillspectorClient("http://jenkins", "admin", "token")
    detector = _detector_with_client(client)
    with patch.object(client, "get_build_status", return_value="BUILDING"):
        result = _await(detector.get_external_result(42))

    assert result["status"] == "pending"
    assert result["build_number"] == 42


def test_get_external_result_not_found():
    from src.security.detector import SkillspectorClient

    client = SkillspectorClient("http://jenkins", "admin", "token")
    detector = _detector_with_client(client)
    with patch.object(client, "get_build_status", return_value="NOT_FOUND"):
        result = _await(detector.get_external_result(42))

    assert result["status"] == "error"
    assert "not found" in result["error"].lower()


def test_get_external_result_report_unavailable():
    from src.security.detector import SkillspectorClient

    client = SkillspectorClient("http://jenkins", "admin", "token")
    detector = _detector_with_client(client)
    with (
        patch.object(client, "get_build_status", return_value="SUCCESS"),
        patch.object(client, "fetch_report", return_value=None),
    ):
        result = _await(detector.get_external_result(42))

    assert result["status"] == "error"


def test_get_external_result_done():
    from src.security.detector import SkillspectorClient

    client = SkillspectorClient("http://jenkins", "admin", "token")
    detector = _detector_with_client(client)
    report = {"risk_assessment": {"severity": "HIGH", "score": 65}}
    report_md = "# Security Report"
    with (
        patch.object(client, "get_build_status", return_value="SUCCESS"),
        patch.object(client, "fetch_report", return_value=report),
        patch.object(client, "fetch_report_md", return_value=report_md),
    ):
        result = _await(detector.get_external_result(42))

    assert result["status"] == "done"
    assert result["risk_level"] == "high"
    assert result["risk_score"] == 65
    assert result["jenkins_status"] == "SUCCESS"
    assert result["details"]["skillspector_report_md"] == report_md


def test_get_external_result_disabled():
    from src.security.detector import SecurityDetector

    detector = SecurityDetector()
    detector._skillspector_client = None
    result = _await(detector.get_external_result(42))
    assert result["status"] == "error"


def test_fetch_external_report_md_returns_markdown():
    from src.security.detector import SkillspectorClient

    client = SkillspectorClient("http://jenkins", "admin", "token")
    detector = _detector_with_client(client)
    report_md = "# Security Report\n\n- high: prompt injection"
    with patch.object(client, "fetch_report_md", return_value=report_md) as mock_fetch:
        md = _await(detector.fetch_external_report_md(42))

    mock_fetch.assert_called_once_with(42)
    assert md == report_md


def test_fetch_external_report_md_disabled():
    from src.security.detector import SecurityDetector

    detector = SecurityDetector()
    detector._skillspector_client = None
    assert _await(detector.fetch_external_report_md(42)) is None


def test_fetch_external_report_md_error_returns_none():
    from src.security.detector import SkillspectorClient

    client = SkillspectorClient("http://jenkins", "admin", "token")
    detector = _detector_with_client(client)
    with patch.object(client, "fetch_report_md", side_effect=RuntimeError("boom")):
        assert _await(detector.fetch_external_report_md(42)) is None


def test_maybe_compress_details_keeps_small_reports_untouched():
    """Issue #10: details ≤1MB are persisted verbatim (zero changes)."""
    from src.security.detector import MAX_DETAILS_BYTES, _maybe_compress_details

    snippet = "c" * 10_000
    details = {
        "skillspector_report": {
            "issues": [
                {
                    "id": "issue-1",
                    "code_snippet": snippet,
                    "location": {"file": "app.py", "start_line": 3},
                }
            ]
        }
    }
    # ensure the fixture is actually below the threshold
    assert len(details["skillspector_report"]["issues"][0]["code_snippet"]) < MAX_DETAILS_BYTES

    result = _maybe_compress_details(details)

    assert result is details
    issue = details["skillspector_report"]["issues"][0]
    assert issue["code_snippet"] == snippet
    assert "truncated" not in issue
    assert issue["location"] == {"file": "app.py", "start_line": 3}


def test_maybe_compress_details_truncates_large_snippets_only():
    """Issue #10: only code_snippet of oversized reports is truncated."""
    from src.security.detector import SNIPPET_MAX_CHARS, _maybe_compress_details

    snippet = "c" * 200_000
    details = {
        "other_key": "x" * 2_000_000,  # 把 details 撑到 >1MB
        "skillspector_report": {
            "risk_assessment": {"severity": "HIGH", "score": 65},
            "issues": [
                {
                    "id": "issue-1",
                    "code_snippet": snippet,
                    "finding": "prompt injection",
                    "remediation": "sanitize input",
                    "explanation": "e" * 300,
                    "location": {"file": "app.py", "start_line": 42},
                },
                {
                    "id": "issue-2",
                    "code_snippet": "short",
                    "location": {"file": "b.py", "start_line": 1},
                },
            ],
        },
    }

    _maybe_compress_details(details)

    report = details["skillspector_report"]
    long_issue = report["issues"][0]
    short_issue = report["issues"][1]
    # 超长 snippet 截断：前 500 字符 + 尾部标记
    assert long_issue["code_snippet"].startswith(snippet[:SNIPPET_MAX_CHARS])
    assert f"original {len(snippet)} chars" in long_issue["code_snippet"]
    assert len(long_issue["code_snippet"]) <= SNIPPET_MAX_CHARS + 60
    assert long_issue["truncated"] is True
    # 其余字段原样保留
    assert long_issue["finding"] == "prompt injection"
    assert long_issue["remediation"] == "sanitize input"
    assert long_issue["location"] == {"file": "app.py", "start_line": 42}
    # 未超长的 issue 不动
    assert short_issue["code_snippet"] == "short"
    assert "truncated" not in short_issue


def test_maybe_compress_details_tolerates_malformed_report():
    """Issue #10: non-dict report / non-list issues / None snippet are skipped."""
    from src.security.detector import _maybe_compress_details

    for details in (
        {"skillspector_report": "not-a-dict", "pad": "x" * 2_000_000},
        {"skillspector_report": {"issues": "not-a-list"}, "pad": "x" * 2_000_000},
        {"skillspector_report": {"issues": [{"code_snippet": None}]}, "pad": "x" * 2_000_000},
        {"skillspector_report": {"issues": [{"code_snippet": 12345}]}, "pad": "x" * 2_000_000},
        {"no_report_key": "x" * 2_000_000},
    ):
        _maybe_compress_details(details)  # 不抛异常即可
        report = details.get("skillspector_report")
        if isinstance(report, dict) and isinstance(report.get("issues"), list):
            for issue in report["issues"]:
                assert "truncated" not in issue


class _AsyncSessionCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def test_collector_failure_isolation_continues_batch():
    """Issue #8: a single failing audit must not break the rest of the batch.

    After ``session.rollback()`` all ORM instances are expired; the collector
    must keep working from snapshot values instead of touching expired
    attributes (MissingGreenlet).
    """
    import asyncio

    from sqlalchemy.dialects import postgresql

    from src.security.detector import SkillspectorClient, SkillspectorCollector

    class FakeAudit:
        def __init__(self, audit_id, resource_id, details):
            self.id = audit_id
            self.resource_type = "skill"
            self.resource_id = resource_id
            self.details = details
            self.risk_signals = []

    def fake_get_build_status(build_number):
        if build_number == 1:
            raise RuntimeError("jenkins boom")
        return "SUCCESS"

    def fake_fetch_report_with_status(build_number):
        return ({"risk_assessment": {"severity": "LOW", "score": 10}}, 200)

    client = SkillspectorClient("http://jenkins", "admin", "token")
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.get = AsyncMock(return_value=None)

    collector = SkillspectorCollector(
        client=client,
        session_factory=lambda: _AsyncSessionCtx(session),
        poll_interval=0,
    )

    audit_1 = FakeAudit(1, "skill-1", {"skillspector_async": True, "skillspector_build_number": 1})
    audit_2 = FakeAudit(2, "skill-2", {"skillspector_async": True, "skillspector_build_number": 2})

    with (
        patch.object(client, "get_build_status", side_effect=fake_get_build_status),
        patch.object(client, "fetch_report_with_status", side_effect=fake_fetch_report_with_status),
        patch.object(collector, "_fetch_pending", new=AsyncMock(return_value=[audit_1, audit_2])),
    ):
        processed = asyncio.run(collector.collect_once())

    # Failure isolated: item 1 rolled back, item 2 still collected.
    assert processed == 1
    session.rollback.assert_awaited_once()

    # 小报告（≤1MB）完整原样入库，skillspector_report 仍保留
    update_stmt = session.execute.await_args_list[0].args[0]
    written = update_stmt.compile(dialect=postgresql.dialect()).params
    stored_report = written["details"]["skillspector_report"]
    assert stored_report["risk_assessment"]["severity"] == "LOW"


def test_collector_reconciles_skill_count_after_collection():
    """审计结果回填 risk_score 后重新对账 contributors.skill_count。

    扫描提交审计时多为 pending（risk_score 为 NULL），count_skills 口径下
    聚合结果是 0；结果收集落地后必须补一次对账，否则贡献者因
    skill_count=0 被页面过滤掉。
    """
    import asyncio

    from src.security.detector import SkillspectorClient, SkillspectorCollector

    class FakeAudit:
        def __init__(self, audit_id, resource_id, details):
            self.id = audit_id
            self.resource_type = "skill"
            self.resource_id = resource_id
            self.details = details
            self.risk_signals = []

    client = SkillspectorClient("http://jenkins", "admin", "token")
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.get = AsyncMock(return_value=MagicMock())

    collector = SkillspectorCollector(
        client=client,
        session_factory=lambda: _AsyncSessionCtx(session),
        poll_interval=0,
    )

    audit = FakeAudit(
        1, "skill-1", {"skillspector_async": True, "skillspector_build_number": 1}
    )

    with (
        patch.object(client, "get_build_status", return_value="SUCCESS"),
        patch.object(
            client,
            "fetch_report_with_status",
            return_value=({"risk_assessment": {"severity": "LOW", "score": 10}}, 200),
        ),
        patch.object(collector, "_fetch_pending", new=AsyncMock(return_value=[audit])),
        patch("src.models.repository.ContributorRepository") as repo_cls,
    ):
        repo_cls.return_value.reconcile_skill_counts = AsyncMock(return_value=2)
        processed = asyncio.run(collector.collect_once())
        assert processed == 1
        repo_cls.return_value.reconcile_skill_counts.assert_awaited_once()

        # 空批次（无结果落地）不做对账
        collector._fetch_pending = AsyncMock(return_value=[])
        asyncio.run(collector.collect_once())
        assert repo_cls.return_value.reconcile_skill_counts.await_count == 1
