"""license 清理任务单测：结果保留、备份清理与配置默认值。"""

import os
import time
import uuid
from datetime import datetime, timezone

from src.core.config import LicenseAuditConfig
from src.jobs.license_audit import LicenseAuditScheduler, _row_dict
from src.models.orm import Skill


def test_config_defaults_match_design():
    config = LicenseAuditConfig()
    assert config.enabled is False
    assert config.start_time == "00:30"
    assert config.dry_run is False
    assert config.actions.conflict == "warning"
    assert config.actions.missing == "failed"
    assert config.safety.max_deletions_per_run == 500
    assert config.safety.protected_repos == ["atomgit.com/openeuler/*"]
    assert config.gate.extra_allow == [
        "CANN-2.0",
        "SUL-1.0",
        "FSL-1.1-Apache-2.0",
        "Convex-CPL",
        "CC-BY-3.0-US",
    ]


def test_prune_results_keeps_latest(tmp_path):
    now = time.time()
    for index in range(5):
        path = tmp_path / f"license-audit-2026010{index}.json"
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (now - index * 86400, now - index * 86400))
    removed = LicenseAuditScheduler._prune_results(tmp_path, keep_days=3, max_files=2)
    assert removed == 3
    assert len(list(tmp_path.glob("license-audit-*.json"))) == 2


def test_prune_backups(tmp_path):
    config = LicenseAuditConfig()
    config.safety.backup_dir = str(tmp_path)
    config.safety.backup_keep_days = 30
    config.safety.backup_keep_files = 2
    scheduler = LicenseAuditScheduler(config)
    now = time.time()
    for index in range(4):
        path = tmp_path / f"2026010{index}.jsonl.gz"
        path.write_bytes(b"x")
        os.utime(path, (now - index * 86400, now - index * 86400))
    scheduler._prune_backups(tmp_path)
    assert len(list(tmp_path.glob("*.jsonl.gz"))) == 2


def test_row_dict_serializes_uuid_and_datetime():
    skill = Skill(
        id=uuid.uuid4(),
        skill_repo_id=uuid.uuid4(),
        skill_id="github:acme/demo",
        name="demo",
        source="github",
        source_url="https://github.com/acme/demo/blob/main/SKILL.md",
        extra_metadata={"license": "MIT"},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    data = _row_dict(skill)
    assert data["skill_id"] == "github:acme/demo"
    assert isinstance(data["id"], str)
    assert data["created_at"].startswith("2026-01-01")
    assert data["extra_metadata"] == {"license": "MIT"}
