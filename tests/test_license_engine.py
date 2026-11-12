"""license 判定引擎与扫描器单测（与门禁同源逻辑）。"""

from pathlib import Path

from src.core.config import LicenseAuditConfig
from src.skills.license.gate_engine import (
    STATUS_ALLOWED,
    STATUS_BLACK,
    STATUS_CONFLICT,
    STATUS_MISSING,
    STATUS_RESTRICTED,
    STATUS_UNKNOWN,
    LicenseAccessList,
    LicenseScanner,
)
from src.skills.license.runtime import action_for_status
from src.skills.license.scanner import derive_skill_dir, is_protected_repo

LICENSE_LIST = """\
Software Licenses:
  Licenses:
  - alias: [MIT, MIT License]
    identifier: MIT
    oeApproved: 'Y'
    lowRisk: 'N'
    black: 'N'
    blackReason: ''
    exception: 'N'
  - alias: [Apache-2.0, Apache License 2.0]
    identifier: Apache-2.0
    oeApproved: 'Y'
    lowRisk: 'N'
    black: 'N'
    blackReason: ''
    exception: 'N'
  - alias: [Mulan PSL v2]
    identifier: MulanPSL-2.0
    oeApproved: 'Y'
    lowRisk: 'N'
    black: 'N'
    blackReason: ''
    exception: 'N'
  - alias: [PostgreSQL License]
    identifier: PostgreSQL
    oeApproved: 'Y'
    lowRisk: 'N'
    black: 'N'
    blackReason: ''
    exception: 'N'
  - alias: [SSPL-1.0, Server Side Public License]
    identifier: SSPL-1.0
    oeApproved: 'N'
    lowRisk: 'N'
    black: 'Y'
    blackReason: 不建议引入，对云厂商不友好
    exception: 'N'
  - alias: [CC-BY-NC-4.0]
    identifier: CC-BY-NC-4.0
    oeApproved: 'N'
    lowRisk: 'Y'
    black: 'N'
    blackReason: ''
    exception: 'N'
"""


def _access(tmp_path: Path, extra_allow=None) -> LicenseAccessList:
    path = tmp_path / "Licenses_Access_List.yaml"
    path.write_text(LICENSE_LIST, encoding="utf-8")
    return LicenseAccessList(source=str(path), extra_allow=extra_allow or []).load()


def _skill_dir(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    skill = repo / "skills" / "demo"
    skill.mkdir(parents=True)
    return repo, skill


def _cann_access(tmp_path: Path) -> LicenseAccessList:
    return _access(tmp_path, extra_allow=["CANN-2.0", "SUL-1.0", "Convex-CPL"])


def test_extra_allow_recognizes_cann(tmp_path):
    repo, skill = _skill_dir(tmp_path)
    (repo / "LICENSE").write_text(
        "CANN Open Software License Agreement Version 2.0\n"
        "This CANN Open Software License Agreement governs use, modification, "
        "or distribution of CANN Open Software.\n",
        encoding="utf-8",
    )
    verdict = LicenseScanner(_cann_access(tmp_path)).scan(repo, skill)
    assert verdict.status == STATUS_ALLOWED
    assert verdict.expression == "CANN-2.0"


def test_extra_allow_never_overrides_black(tmp_path):
    access = _access(tmp_path, extra_allow=["SSPL-1.0"])
    assert access.classify("SSPL-1.0")[0] == STATUS_BLACK


def test_placeholder_declared_falls_back_to_file(tmp_path):
    repo, skill = _skill_dir(tmp_path)
    (repo / "LICENSE").write_text(
        "MIT License\n\nPermission is hereby granted, free of charge, to any person "
        "obtaining a copy of this software.\n",
        encoding="utf-8",
    )
    verdict = LicenseScanner(_access(tmp_path), prefer_file=True).scan(
        repo, skill, "Complete terms in LICENSE.txt"
    )
    assert verdict.status == STATUS_ALLOWED
    assert verdict.expression == "MIT"
    assert "无法解析" in verdict.reasons[0]


def test_placeholder_declared_without_prefer_file_stays_unknown(tmp_path):
    repo, skill = _skill_dir(tmp_path)
    (repo / "LICENSE").write_text(
        "MIT License\n\nPermission is hereby granted, free of charge.\n",
        encoding="utf-8",
    )
    verdict = LicenseScanner(_access(tmp_path)).scan(
        repo, skill, "Complete terms in LICENSE.txt"
    )
    assert verdict.status == STATUS_UNKNOWN


def test_single_token_unresolvable_declared_stays_unknown(tmp_path):
    repo, skill = _skill_dir(tmp_path)
    (repo / "LICENSE").write_text(
        "MIT License\n\nPermission is hereby granted, free of charge.\n",
        encoding="utf-8",
    )
    verdict = LicenseScanner(_access(tmp_path), prefer_file=True).scan(
        repo, skill, "SSPL"
    )
    assert verdict.status == STATUS_UNKNOWN


def test_conflict_between_declared_and_file(tmp_path):
    repo, skill = _skill_dir(tmp_path)
    (repo / "LICENSE").write_text(
        "MIT License\n\nPermission is hereby granted, free of charge.\n",
        encoding="utf-8",
    )
    verdict = LicenseScanner(_access(tmp_path)).scan(repo, skill, "Apache-2.0")
    assert verdict.status == STATUS_CONFLICT
    assert "不一致" in verdict.reasons[0]


def test_prefer_file_for_restricted_declared(tmp_path):
    repo, skill = _skill_dir(tmp_path)
    (repo / "LICENSE").write_text(
        "MIT License\n\nPermission is hereby granted, free of charge.\n",
        encoding="utf-8",
    )
    strict = LicenseScanner(_access(tmp_path)).scan(repo, skill, "CC-BY-NC-4.0")
    assert strict.status == STATUS_RESTRICTED

    prefer = LicenseScanner(_access(tmp_path), prefer_file=True).scan(
        repo, skill, "CC-BY-NC-4.0"
    )
    assert prefer.status == STATUS_ALLOWED
    assert prefer.expression == "MIT"


def test_prefer_file_keeps_black(tmp_path):
    repo, skill = _skill_dir(tmp_path)
    (repo / "LICENSE").write_text(
        "MIT License\n\nPermission is hereby granted, free of charge.\n",
        encoding="utf-8",
    )
    verdict = LicenseScanner(_access(tmp_path), prefer_file=True).scan(
        repo, skill, "SSPL-1.0"
    )
    assert verdict.status == STATUS_BLACK


def test_detects_mulan_and_postgresql(tmp_path):
    repo, skill = _skill_dir(tmp_path)
    (skill / "LICENSE").write_text(
        "oebuild is licensed under Mulan PSL v2.\n", encoding="utf-8"
    )
    verdict = LicenseScanner(_access(tmp_path)).scan(repo, skill)
    assert verdict.status == STATUS_ALLOWED
    assert verdict.expression == "MulanPSL-2.0"

    (skill / "LICENSE").write_text("PostgreSQL License\n", encoding="utf-8")
    verdict = LicenseScanner(_access(tmp_path)).scan(repo, skill)
    assert verdict.status == STATUS_ALLOWED
    assert verdict.expression == "PostgreSQL"


def test_missing_and_unknown(tmp_path):
    repo, skill = _skill_dir(tmp_path)
    scanner = LicenseScanner(_access(tmp_path))
    assert scanner.scan(repo, skill).status == STATUS_MISSING

    (skill / "LICENSE").write_text("Some custom terms.", encoding="utf-8")
    assert scanner.scan(repo, skill).status == STATUS_UNKNOWN


def test_action_mapping_matches_gate_defaults():
    config = LicenseAuditConfig()
    assert action_for_status(config, STATUS_ALLOWED) == "success"
    assert action_for_status(config, STATUS_CONFLICT) == "warning"
    assert action_for_status(config, STATUS_MISSING) == "failed"
    assert action_for_status(config, STATUS_UNKNOWN) == "failed"
    assert action_for_status(config, STATUS_RESTRICTED) == "failed"
    assert action_for_status(config, STATUS_BLACK) == "failed"


def test_protected_repo_matching():
    patterns = ["atomgit.com/openeuler/*"]
    assert is_protected_repo("https://atomgit.com/openeuler/witty-agents", patterns)
    assert not is_protected_repo("https://atomgit.com/cann/cannbot-skills", patterns)
    assert not is_protected_repo("https://github.com/openeuler/skills", patterns)


def test_derive_skill_dir():
    assert (
        derive_skill_dir(
            "https://github.com/openai/plugins/blob/main/plugins/foo/skills/bar/SKILL.md"
        )
        == "plugins/foo/skills/bar"
    )
    assert (
        derive_skill_dir(
            "https://raw.githubusercontent.com/acme/repo/main/skills/demo/SKILL.md"
        )
        == "skills/demo"
    )
    assert derive_skill_dir("") == ""
