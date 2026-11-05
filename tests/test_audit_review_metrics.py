"""LLM 复核指标汇总与结果文件测试。"""

import os
import time

from src.core.config import AuditLLMReviewConfig
from src.security.audit_review.metrics import (
    AuditRecord,
    RunStats,
    prune_result_files,
    resolve_result_dir,
    save_run_result,
)


def make_config(tmp_path) -> AuditLLMReviewConfig:
    return AuditLLMReviewConfig(api_key="test", result_dir=str(tmp_path))


def test_run_stats_document_aggregates(tmp_path):
    stats = RunStats("run-1", make_config(tmp_path))
    reviewed = AuditRecord(
        audit_id="audit-ok",
        skill_id="community:demo/ok",
        name="demo-ok",
        risk_level="medium",
        issues_count=3,
        outcome="reviewed",
        reviewed=3,
        confirmed=1,
        false_positive=2,
        score_before=30,
        score_after=10,
        level_before="medium",
        level_after="low",
        duration_seconds=1.5,
        context_source="cache",
        llm_calls=2,
        llm_retries=1,
        estimated_tokens=1000,
        llm_seconds=2.0,
    )
    reviewed.false_positive_rules["PE2"] = 2
    failed = AuditRecord(
        audit_id="audit-fail",
        outcome="failed",
        duration_seconds=0.5,
        context_source="snippet",
        context_degraded=True,
        context_degrade_reason="cache_file_missing+remote_disabled",
        error="LLMReviewError: boom",
        error_type="LLMReviewError",
    )
    stats.record(reviewed)
    stats.record(failed)
    stats.stopped = "no_work"

    document = stats.document()

    assert document["counters"]["reviewed"] == 1
    assert document["counters"]["failed"] == 1
    assert document["counters"]["total"] == 2
    assert document["counters"]["reviewed_note"]
    assert document["llm"]["calls"] == 2
    assert document["llm"]["retries"] == 1
    assert document["llm"]["slow_calls"] == 0
    assert document["context"]["sources"] == {"cache": 1, "snippet": 1}
    assert document["context"]["reasons"] == {
        "cache_file_missing+remote_disabled": 1
    }
    assert document["false_positive_rules"] == {"PE2": 2}
    assert [item["audit_id"] for item in document["failed_audits"]] == ["audit-fail"]
    assert document["window"]["timezone"]
    assert document["model"]["context_mode"] == "window"


def test_prune_result_files_by_count(tmp_path):
    now = time.time()
    for index in range(3):
        path = tmp_path / f"audit-review-2026010{index + 1}-000000.json"
        path.write_text("{}", encoding="utf-8")
        stamp = now - (3 - index) * 100
        os.utime(path, (stamp, stamp))

    removed = prune_result_files(tmp_path, keep_days=0, max_files=2)

    assert removed == 1
    assert len(list(tmp_path.glob("audit-review-*.json"))) == 2
    assert not (tmp_path / "audit-review-20260101-000000.json").exists()


def test_prune_result_files_by_age(tmp_path):
    old = tmp_path / "audit-review-old.json"
    old.write_text("{}", encoding="utf-8")
    past = time.time() - 40 * 86400
    os.utime(old, (past, past))
    fresh = tmp_path / "audit-review-fresh.json"
    fresh.write_text("{}", encoding="utf-8")

    removed = prune_result_files(tmp_path, keep_days=30, max_files=200)

    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_resolve_result_dir_prefers_configured_path(tmp_path):
    assert resolve_result_dir(str(tmp_path)) == tmp_path


def test_save_run_result_writes_json(tmp_path):
    config = make_config(tmp_path)

    path = save_run_result({"run_id": "run-1"}, config)

    assert path is not None
    assert path.parent == tmp_path
    assert path.name.startswith("audit-review-")
    assert path.name.endswith(".json")
    assert path.read_text(encoding="utf-8").strip().startswith("{")
