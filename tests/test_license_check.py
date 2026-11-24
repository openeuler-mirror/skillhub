"""license 三态标记链路的回归测试。

覆盖范围：

1. **状态映射**（``verdict_to_flag``）：allowed -> 1；restricted/black/
   missing/unknown -> 0；error -> None（不写库）。
2. **判定优先级**：SKILL.md 声明优先于 LICENSE 文件；声明缺失才回退文件；
   声明 restricted + 文件 permissive 仍是 0（冲突只写进 reasons）。
3. **目录级 LICENSE 缓存**：同一仓库多个 skill 只 listing 一次父目录。
4. **定时任务判定**：``_check_rows`` 按仓库分组、(skill_dir, declared) 复用
   结论、列表声明可哈希、单条异常降级为 None。
5. **可见性**：详情/审计接口/下载对 ``license_compliant == 0`` 返回 404；
   NULL 仍可见。
6. **路径边界**：skill 目录越出仓库根（``..`` / 绝对路径）按 error 处理，
   不读取仓库外的 LICENSE。
7. **无工作树 clone**：discover 的 ``--no-checkout`` clone 从 git 对象库
   物化 license 文件（``local_tree``），避免误判为 missing。
8. **缓存回收**：远程与 ``_local`` 缓存按保留期删除，active 目录、无 marker
   目录、非本模块目录保留；无 marker 的半成品远程缓存不被视为新鲜。

这些用例不依赖真实数据库与网络：判定用本地临时目录，任务与路由用
``SimpleNamespace`` 桩对象 + monkeypatch。
"""

import asyncio
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from src.api.routes.skills import (
    audit_skill,
    download_skill,
    get_skill,
    get_skill_audit_report,
)
from src.jobs.discover_scheduler import DiscoverScheduler
from src.jobs.license_check import LicenseCheckScheduler, LicenseRowResult
from src.skills.license.gate_engine import (
    STATUS_ALLOWED,
    STATUS_ERROR,
    STATUS_MISSING,
    STATUS_RESTRICTED,
    STATUS_UNKNOWN,
    LicenseAccessList,
    LicenseScanner,
    LicenseVerdict,
)
from src.skills.license.runtime import verdict_to_flag, verdict_to_license
from src.skills.license.scanner import LicenseChecker, LicenseCheckResult
from src.skills.skill_scanner import SkillScanner

ACCESS_LIST = """
Software Licenses:
  Permissive:
    - identifier: MIT
      alias: ["MIT License"]
      oeApproved: Y
      lowRisk: Y
    - identifier: Apache-2.0
      alias: ["Apache License 2.0"]
      oeApproved: Y
      lowRisk: Y
  Restricted:
    - identifier: GPL-3.0-only
      alias: ["GPL-3.0"]
      oeApproved: N
      lowRisk: Y
"""

MIT_TEXT = (
    "MIT License\n\n"
    "Permission is hereby granted, free of charge, to any person obtaining a copy "
    "of this software and associated documentation files (the \"Software\")."
)
APACHE_TEXT = "Apache License\nVersion 2.0, January 2004"
GPL_TEXT = "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007"


def _build_scanner(tmp_path):
    access_file = tmp_path / "access.yaml"
    access_file.write_text(ACCESS_LIST, encoding="utf-8")
    access = LicenseAccessList(source=str(access_file)).load()
    return LicenseScanner(access)


def _build_checker(tmp_path, remote_cache_root: str = ""):
    access_file = tmp_path / "access.yaml"
    access_file.write_text(ACCESS_LIST, encoding="utf-8")
    access = LicenseAccessList(source=str(access_file)).load()
    return LicenseChecker(access, remote_cache_root=remote_cache_root)


def _write_license(tmp_path, text):
    (tmp_path / "LICENSE").write_text(text, encoding="utf-8")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_verdict_to_flag_mapping():
    assert verdict_to_flag(STATUS_ALLOWED) == 1
    assert verdict_to_flag(STATUS_UNKNOWN) == 0
    assert verdict_to_flag(STATUS_MISSING) == 0
    assert verdict_to_flag("black") == 0
    assert verdict_to_flag("restricted") == 0
    assert verdict_to_flag(STATUS_ERROR) is None


def test_verdict_to_license_mapping():
    """license 类型与三态来自同一次判定：表达式非空即落库。"""
    assert verdict_to_license(LicenseVerdict(STATUS_ALLOWED, expression="MIT")) == "MIT"
    assert (
        verdict_to_license(
            LicenseVerdict(STATUS_ALLOWED, expression="MIT AND Apache-2.0")
        )
        == "MIT AND Apache-2.0"
    )
    # unknown 保留原始声明，便于回溯“为什么判成不合规”
    assert (
        verdict_to_license(LicenseVerdict(STATUS_UNKNOWN, expression="Not-A-Real-License"))
        == "Not-A-Real-License"
    )
    # missing / error 没有可记录的 license 类型
    assert verdict_to_license(LicenseVerdict(STATUS_MISSING)) is None
    assert verdict_to_license(LicenseVerdict(STATUS_ERROR)) is None


def test_conflict_uses_declared_license(tmp_path):
    """声明 MIT、LICENSE 为 Apache-2.0 时按声明判为合规，并记录冲突原因。"""
    _write_license(tmp_path, APACHE_TEXT)
    scanner = _build_scanner(tmp_path)

    verdict = scanner.scan(tmp_path, tmp_path, "MIT")

    assert verdict.status == STATUS_ALLOWED
    assert verdict.expression == "MIT"
    assert any("不一致" in reason for reason in verdict.reasons)
    assert verdict_to_flag(verdict.status) == 1


def test_extra_restricted_license_does_not_override_declaration(tmp_path):
    """LICENSE 混入声明之外的 GPL 时仍以声明为准，但记录合规风险。"""
    _write_license(tmp_path, MIT_TEXT + "\n" + GPL_TEXT)
    scanner = _build_scanner(tmp_path)

    verdict = scanner.scan(tmp_path, tmp_path, "MIT")

    assert verdict.status == STATUS_ALLOWED
    assert any("GPL-3.0-only" in reason for reason in verdict.reasons)
    assert verdict_to_flag(verdict.status) == 1


def test_declared_restricted_is_not_rescued_by_license_file(tmp_path):
    """声明 GPL、文件为 MIT 时仍按声明判为不合规。"""
    _write_license(tmp_path, MIT_TEXT)
    scanner = _build_scanner(tmp_path)

    verdict = scanner.scan(tmp_path, tmp_path, "GPL-3.0-only")

    assert verdict.status == STATUS_RESTRICTED
    assert any("不一致" in reason for reason in verdict.reasons)
    assert verdict_to_flag(verdict.status) == 0


def test_missing_license_is_non_compliant(tmp_path):
    scanner = _build_scanner(tmp_path)

    verdict = scanner.scan(tmp_path, tmp_path, None)

    assert verdict.status == STATUS_MISSING
    assert verdict_to_flag(verdict.status) == 0


def test_unknown_declared_license_is_non_compliant(tmp_path):
    scanner = _build_scanner(tmp_path)

    verdict = scanner.scan(tmp_path, tmp_path, "Not-A-Real-License")

    assert verdict.status == STATUS_UNKNOWN
    assert verdict_to_flag(verdict.status) == 0


def test_scanner_accepts_list_declaration(tmp_path):
    """frontmatter 的 license 列表（YAML 块列表/行内列表）按 AND 判定。"""
    _write_license(tmp_path, APACHE_TEXT)
    scanner = _build_scanner(tmp_path)

    verdict = scanner.scan(tmp_path, tmp_path, ["MIT", "Apache-2.0"])

    assert verdict.status == STATUS_ALLOWED
    assert verdict.expression == "MIT AND Apache-2.0"


def test_license_lookup_caches_directory_listing(tmp_path, monkeypatch):
    """同一仓库多个 skill 共享父目录时，父目录只 listing 一次。"""
    _write_license(tmp_path, MIT_TEXT)
    (tmp_path / "skills" / "a").mkdir(parents=True)
    (tmp_path / "skills" / "b").mkdir(parents=True)
    parent = (tmp_path / "skills").resolve()
    calls: list[Path] = []
    original_iterdir = Path.iterdir

    def counting_iterdir(self):
        if self == parent:
            calls.append(self)
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)
    scanner = _build_scanner(tmp_path)

    first = scanner.scan(tmp_path, tmp_path / "skills" / "a", "MIT")
    second = scanner.scan(tmp_path, tmp_path / "skills" / "b", "MIT")

    assert first.status == STATUS_ALLOWED
    assert second.status == STATUS_ALLOWED
    assert len(calls) == 1


def test_local_clone_without_worktree_materializes_license(tmp_path):
    """discover 的 --no-checkout clone 没有工作树，必须从 git 对象库物化 license。"""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q")
    _git(origin, "config", "user.email", "test@example.com")
    _git(origin, "config", "user.name", "Test")
    _write_license(origin, MIT_TEXT)
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", "init")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--no-checkout", "-q", str(origin), str(clone))
    # 确认工作树为空（只有 .git）
    assert not (clone / "LICENSE").exists()
    assert not (clone / "SKILL.md").exists()

    checker = _build_checker(tmp_path)
    try:
        result = checker.check("", str(clone), "", None)
    finally:
        checker.close()

    assert result.repo_source == "local_tree"
    assert result.verdict.status == STATUS_ALLOWED


def test_fetch_license_tree_rejects_partial_result(monkeypatch, tmp_path):
    """远程轻量拉取只取到部分 license 文件时不能落成“完整”缓存。"""
    checker = _build_checker(tmp_path, remote_cache_root=str(tmp_path / "cache"))
    listing = "100644 blob a\tLICENSE\n100644 blob b\tCOPYING\n"
    monkeypatch.setattr(checker, "_git", lambda args: True)
    monkeypatch.setattr(checker, "_git_output", lambda args: (0, listing))
    monkeypatch.setattr(
        checker,
        "_git_bytes",
        lambda args: (0, b"MIT License") if args[-1].endswith("LICENSE") else (1, b""),
    )
    cache_dir = tmp_path / "cache" / "example"
    try:
        assert checker._fetch_license_tree("https://example.com/repo", cache_dir) is False
    finally:
        checker.close()


def test_prune_remote_cache_removes_old_and_keeps_active(tmp_path):
    """远程缓存按保留期回收；本轮 active 的 key 与 _local 目录不删。"""
    root = tmp_path / "cache"
    root.mkdir()
    marker = ".license_fetched_at"
    old = root / "old-repo"
    fresh = root / "fresh-repo"
    active = root / "active-repo"
    for directory in (old, fresh, active):
        directory.mkdir()
        (directory / marker).write_text("x", encoding="utf-8")
    local = root / "_local"
    local.mkdir()
    (local / "repo-0").mkdir()
    # 没有 marker 的目录可能是误配的共享目录，不应被删除
    unrelated = root / "unrelated"
    unrelated.mkdir()
    (unrelated / "data.txt").write_text("keep", encoding="utf-8")

    old_ts = time.time() - 10 * 86400
    for directory in (old, active, unrelated):
        os.utime(directory, (old_ts, old_ts))
        marker_path = directory / marker
        if marker_path.is_file():
            os.utime(marker_path, (old_ts, old_ts))

    checker = _build_checker(tmp_path, remote_cache_root=str(root))
    removed = checker.prune_remote_cache(7, active_keys={"active-repo"})

    assert removed == 1
    assert not old.exists()
    assert fresh.exists()
    assert active.exists()
    assert local.exists()
    assert unrelated.exists()


def test_prune_local_cache_removes_old_unused_and_keeps_active(tmp_path):
    """_local 缓存按保留期回收；本轮物化过的目录与无 marker/非本模块目录不删。"""
    root = tmp_path / "cache"
    local = root / "_local"
    local.mkdir(parents=True)
    marker = ".license_fetched_at"
    # 合法缓存目录名为 16 位 sha1 前缀
    old = local / ("a" * 16)
    active = local / ("b" * 16)
    fresh = local / ("c" * 16)
    for directory in (old, active, fresh):
        directory.mkdir()
        (directory / marker).write_text("x", encoding="utf-8")
    # 物化中断残留（无 marker）与误放在 _local 下的无关目录，都不应删除
    incomplete = local / ("d" * 16)
    incomplete.mkdir()
    unrelated = local / "backup"
    unrelated.mkdir()

    old_ts = time.time() - 10 * 86400
    for directory in (old, active, incomplete, unrelated):
        os.utime(directory, (old_ts, old_ts))
        marker_path = directory / marker
        if marker_path.is_file():
            os.utime(marker_path, (old_ts, old_ts))

    checker = _build_checker(tmp_path, remote_cache_root=str(root))
    checker._local_trees = {"/repo/active": active}
    removed = checker.prune_local_cache(7)

    assert removed == 1
    assert not old.exists()
    assert active.exists()
    assert fresh.exists()
    assert incomplete.exists()
    assert unrelated.exists()


def test_remote_cache_without_marker_is_not_fresh(tmp_path):
    """拉取中断的半成品远程缓存（无 marker）不得复用，需重新拉取。"""
    root = tmp_path / "cache"
    partial = root / "github.com_acme_demo"
    partial.mkdir(parents=True)
    (partial / "LICENSE").write_text(MIT_TEXT, encoding="utf-8")

    checker = _build_checker(tmp_path, remote_cache_root=str(root))
    assert checker._cache_fresh(partial) is False

    # 完整缓存才有 marker；有 marker 且在 TTL 内可复用
    (partial / ".license_fetched_at").write_text(str(int(time.time())), encoding="utf-8")
    assert checker._cache_fresh(partial) is True


@pytest.mark.asyncio
async def test_job_prune_caches_passes_active_repo_keys():
    """任务清理时把本轮仓库 key 传给远程缓存清理，避免误删活跃缓存。"""

    class FakeChecker:
        def __init__(self):
            self.remote_args = None
            self.local_days = None

        def prune_remote_cache(self, keep_days, active_keys):
            self.remote_args = (keep_days, active_keys)
            return 1

        def prune_local_cache(self, keep_days):
            self.local_days = keep_days
            return 2

    checker = FakeChecker()
    scheduler = LicenseCheckScheduler(
        SimpleNamespace(
            timezone="Asia/Shanghai",
            start_time="02:00",
            batch_size=200,
            repo=SimpleNamespace(
                remote_concurrency=4,
                remote_cache_keep_days=7,
            ),
        )
    )
    rows = [
        {"url": "https://github.com/acme/demo", "local_path": "/data/demo"},
        {"url": "https://github.com/acme/demo", "local_path": "/data/demo"},
        {"url": "", "local_path": "/data/local-only"},
    ]

    await scheduler._prune_caches(checker, rows)

    assert checker.remote_args == (7, {"github.com_acme_demo"})
    assert checker.local_days == 7


@pytest.mark.asyncio
async def test_job_check_rows_groups_repo_and_reuses_verdicts(tmp_path):
    class FakeChecker:
        def __init__(self):
            self.calls = []

        def check(self, repo_url, local_path, skill_dir, declared):
            self.calls.append((repo_url, local_path, skill_dir, declared))
            if repo_url == "https://example.com/boom":
                raise RuntimeError("boom")
            return LicenseCheckResult(
                LicenseVerdict(STATUS_ALLOWED, expression="MIT"), "local"
            )

    checker = FakeChecker()
    scheduler = LicenseCheckScheduler(
        SimpleNamespace(
            timezone="Asia/Shanghai",
            start_time="02:00",
            batch_size=200,
            repo=SimpleNamespace(remote_concurrency=4),
        )
    )
    rows = [
        {
            "id": "1",
            "table": "skills",
            "skill_id": "a",
            "source_url": "https://example.com/a/SKILL.md",
            "extra_metadata": {"license": "MIT"},
            "url": "https://example.com/a",
            "local_path": None,
        },
        {
            "id": "2",
            "table": "skill_versions",
            "skill_id": "a",
            "source_url": "https://example.com/a/SKILL.md",
            "extra_metadata": {"license": "MIT"},
            "url": "https://example.com/a",
            "local_path": None,
        },
        {
            "id": "3",
            "table": "skills",
            "skill_id": "b",
            "source_url": "https://example.com/b/SKILL.md",
            "extra_metadata": {"license": "MIT"},
            "url": "https://example.com/b",
            "local_path": None,
        },
        {
            "id": "4",
            "table": "skills",
            "skill_id": "c",
            "source_url": "https://example.com/boom/SKILL.md",
            "extra_metadata": {},
            "url": "https://example.com/boom",
            "local_path": None,
        },
    ]

    results = await scheduler._check_rows(checker, rows)

    flags = {item.row["id"]: item.flag for item in results}
    assert flags == {"1": 1, "2": 1, "3": 1, "4": None}
    # license 类型与三态同一次判定产出；异常行不携带类型
    assert {item.row["id"]: item.license for item in results}["1"] == "MIT"
    assert results[-1].license is None
    # 同一 repo + skill_dir + 声明只判定一次（rows 1/2 复用）
    assert len([call for call in checker.calls if call[0] == "https://example.com/a"]) == 1


@pytest.mark.parametrize("bad_dir", ["../outside", "__ABS__"])
def test_check_local_rejects_skill_dir_outside_repo(tmp_path, bad_dir):
    """skill_dir 越界（.. 或绝对路径）时按 error 处理，不读取仓库外 LICENSE。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "LICENSE").write_text(MIT_TEXT, encoding="utf-8")
    if bad_dir == "__ABS__":
        bad_dir = str(outside)

    checker = _build_checker(tmp_path)
    result = checker.check_local(repo, bad_dir, "MIT")

    assert result.verdict.status == STATUS_ERROR
    assert result.repo_source == "error"
    assert verdict_to_flag(result.verdict.status) is None


@pytest.mark.asyncio
async def test_job_check_rows_accepts_list_license_declaration():
    """frontmatter 允许 license 列表；缓存 key 不能因不可哈希而崩溃。"""

    class FakeChecker:
        def __init__(self):
            self.calls = 0

        def check(self, repo_url, local_path, skill_dir, declared):
            self.calls += 1
            return LicenseCheckResult(LicenseVerdict(STATUS_ALLOWED), "local")

    checker = FakeChecker()
    scheduler = LicenseCheckScheduler(
        SimpleNamespace(
            timezone="Asia/Shanghai",
            start_time="02:00",
            batch_size=200,
            repo=SimpleNamespace(remote_concurrency=4),
        )
    )
    rows = [
        {
            "id": "1",
            "table": "skills",
            "skill_id": "a",
            "source_url": "https://example.com/a/SKILL.md",
            "extra_metadata": {"license": ["MIT", "Apache-2.0"]},
            "url": "https://example.com/a",
            "local_path": None,
        },
        {
            "id": "2",
            "table": "skill_versions",
            "skill_id": "a",
            "source_url": "https://example.com/a/SKILL.md",
            "extra_metadata": {"license": ["MIT", "Apache-2.0"]},
            "url": "https://example.com/a",
            "local_path": None,
        },
    ]

    results = await scheduler._check_rows(checker, rows)

    assert {item.row["id"]: item.flag for item in results} == {"1": 1, "2": 1}
    # 列表声明归一化后可哈希，同一目录/声明只判定一次
    assert checker.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [audit_skill, get_skill_audit_report])
async def test_audit_endpoints_hidden_when_non_compliant(handler):
    """审计接口与详情同口径：license=0 时不暴露安全信息。"""
    repo = MagicMock()
    repo.get_by_skill_id = AsyncMock(
        return_value=SimpleNamespace(license_compliant=0)
    )
    with patch("src.api.routes.skills.SkillRepository", return_value=repo):
        with pytest.raises(HTTPException) as exc_info:
            await handler(skill_id="github:acme/demo", db=AsyncMock())

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_skill_detail_hidden_when_non_compliant():
    repo = MagicMock()
    repo.get_by_skill_id = AsyncMock(
        return_value=SimpleNamespace(license_compliant=0)
    )
    with patch("src.api.routes.skills.SkillRepository", return_value=repo):
        with pytest.raises(HTTPException) as exc_info:
            await get_skill(skill_id="github:acme/demo", db=AsyncMock())

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_skill_detail_visible_when_unchecked():
    repo = MagicMock()
    repo.get_by_skill_id = AsyncMock(
        return_value=SimpleNamespace(license_compliant=None)
    )
    with patch("src.api.routes.skills.SkillRepository", return_value=repo), patch(
        "src.api.routes.skills.skill_to_response", return_value=MagicMock()
    ) as mapper:
        response = await get_skill(skill_id="github:acme/demo", db=AsyncMock())

    assert response is mapper.return_value


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "1.0.0"])
async def test_skill_download_hidden_when_non_compliant(version):
    repo = MagicMock()
    hidden = SimpleNamespace(license_compliant=0, skill_repo=MagicMock())
    repo.get_with_repository_by_skill_id = AsyncMock(return_value=hidden)
    repo.get_version_with_repository = AsyncMock(return_value=hidden)
    with patch("src.api.routes.skills.SkillRepository", return_value=repo):
        with pytest.raises(HTTPException) as exc_info:
            await download_skill(
                skill_id="github:acme/demo",
                request=MagicMock(),
                version=version,
                db=AsyncMock(),
            )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "1.0.0"])
async def test_skill_download_allows_unchecked(version):
    repo = MagicMock()
    unchecked = SimpleNamespace(license_compliant=None, skill_repo=None)
    repo.get_with_repository_by_skill_id = AsyncMock(return_value=unchecked)
    repo.get_version_with_repository = AsyncMock(return_value=unchecked)
    with patch("src.api.routes.skills.SkillRepository", return_value=repo):
        with pytest.raises(HTTPException) as exc_info:
            await download_skill(
                skill_id="github:acme/demo",
                request=MagicMock(),
                version=version,
                db=AsyncMock(),
            )

    # 通过了 license 拦截，停在仓库元数据校验
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_job_persist_writes_license_type_with_flag():
    """落库时 license 与 license_compliant 成对写入，未检查行不覆盖原值。"""
    scheduler = LicenseCheckScheduler(
        SimpleNamespace(
            timezone="Asia/Shanghai",
            start_time="02:00",
            batch_size=200,
            repo=SimpleNamespace(remote_concurrency=4, remote_cache_keep_days=7),
        )
    )
    session = AsyncMock()
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    contributor_repo = SimpleNamespace(
        reconcile_skill_counts=AsyncMock(return_value=0)
    )
    results = [
        # 首次得到结论：写入 flag + license
        LicenseRowResult(
            {"id": "1", "table": "skills", "license": None, "license_compliant": None},
            1,
            "MIT",
        ),
        # 仅 license 类型变化也要更新
        LicenseRowResult(
            {"id": "2", "table": "skills", "license": "MIT", "license_compliant": 1},
            1,
            "Apache-2.0",
        ),
        # 两列都未变化：跳过写入
        LicenseRowResult(
            {
                "id": "3",
                "table": "skill_versions",
                "license": "MIT",
                "license_compliant": 1,
            },
            1,
            "MIT",
        ),
        # 未完成检查：保持原值，不写库
        LicenseRowResult(
            {
                "id": "4",
                "table": "skill_versions",
                "license": "GPL-3.0-only",
                "license_compliant": 0,
            },
            None,
            None,
        ),
    ]

    with (
        patch("src.jobs.license_check.AsyncSessionLocal", return_value=session_ctx),
        patch(
            "src.jobs.license_check.ContributorRepository",
            return_value=contributor_repo,
        ),
    ):
        counters = await scheduler._persist(results)

    assert counters == {"compliant": 3, "non_compliant": 0, "errors": 1}
    assert session.execute.await_count == 2
    written = []
    for call in session.execute.await_args_list:
        compiled = call.args[0].compile(dialect=postgresql.dialect())
        written.append((compiled.params["license_compliant"], compiled.params["license"]))
    assert sorted(written) == [(1, "Apache-2.0"), (1, "MIT")]


@pytest.mark.asyncio
async def test_discover_precheck_returns_license_and_reuses_cache(tmp_path):
    """discover 预检：返回 license 类型/三态，并按 (目录, 声明) 复用结论。"""

    class FakeChecker:
        def __init__(self):
            self.calls = []

        def check(self, repo_url, local_path, skill_dir, declared):
            self.calls.append((repo_url, skill_dir, declared))
            return LicenseCheckResult(
                LicenseVerdict(STATUS_ALLOWED, expression="MIT"), "local"
            )

    checker = FakeChecker()
    scanner = SkillScanner(
        git_ops=MagicMock(),
        skill_repository=MagicMock(),
        license_checker=checker,
    )
    repo = SimpleNamespace(url="https://example.com/acme/demo")

    first = await scanner._resolve_license_result(
        repo=repo,
        repo_root=tmp_path,
        relative_path="skills/a/SKILL.md",
        declared="MIT",
    )
    second = await scanner._resolve_license_result(
        repo=repo,
        repo_root=tmp_path,
        relative_path="skills/a/SKILL.md",
        declared="MIT",
    )

    assert first == ("MIT", 1, True)
    assert second == first
    assert checker.calls == [("https://example.com/acme/demo", "skills/a", "MIT")]


@pytest.mark.asyncio
async def test_discover_precheck_failure_keeps_fields_null(tmp_path):
    """预检异常不写入结论（交给定时任务重试），不缓存失败结果。"""

    class BoomChecker:
        def __init__(self):
            self.calls = 0

        def check(self, repo_url, local_path, skill_dir, declared):
            self.calls += 1
            raise RuntimeError("repo unavailable")

    checker = BoomChecker()
    scanner = SkillScanner(
        git_ops=MagicMock(),
        skill_repository=MagicMock(),
        license_checker=checker,
    )
    repo = SimpleNamespace(url="https://example.com/acme/demo")

    for _ in range(2):
        result = await scanner._resolve_license_result(
            repo=repo,
            repo_root=tmp_path,
            relative_path="skills/a/SKILL.md",
            declared="MIT",
        )
        assert result == (None, None, False)

    # 失败不缓存：两次调用都会重试
    assert checker.calls == 2


@pytest.mark.asyncio
async def test_license_scheduler_runs_after_each_discover_round():
    """license 调度器跟随 discover 轮次：每轮结束后执行一次，不并发重入。"""
    discover = DiscoverScheduler(
        SimpleNamespace(
            enabled=True,
            start_time="00:00",
            timezone="Asia/Shanghai",
            result_dir="",
        )
    )
    scheduler = LicenseCheckScheduler(
        SimpleNamespace(
            timezone="Asia/Shanghai",
            start_time="02:00",
            batch_size=200,
            repo=SimpleNamespace(remote_concurrency=4, remote_cache_keep_days=7),
        ),
        discover_scheduler=discover,
    )
    ran = asyncio.Event()
    calls: list[int] = []

    async def fake_run_once():
        calls.append(1)
        ran.set()

    scheduler.run_once = fake_run_once  # type: ignore[method-assign]
    await scheduler.start()
    try:
        # 还没有 discover 轮次结束：不应提前执行
        await asyncio.sleep(0.01)
        assert calls == []

        await discover._notify_round_finished()
        await asyncio.wait_for(ran.wait(), timeout=1)
        assert len(calls) == 1

        # 每一轮 discover 结束都会再触发一次，直到 stop 取消
        ran.clear()
        await discover._notify_round_finished()
        await asyncio.wait_for(ran.wait(), timeout=1)
        assert len(calls) == 2
    finally:
        await scheduler.stop()
