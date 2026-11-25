"""license 三态标记链路的回归测试。

覆盖范围：

1. **状态映射**（``verdict_to_flag``）：allowed -> 1；restricted/black/
   missing/unknown -> 0；error -> None（不写库）。
2. **判定优先级**：SKILL.md 声明优先于 LICENSE 文件；声明缺失才回退文件；
   声明 restricted + 文件 permissive 仍是 0（冲突只写进 reasons）。
3. **目录级 LICENSE 缓存**：同一仓库多个 skill 只 listing 一次父目录。
4. **路径边界**：skill 目录越出仓库根（``..`` / 绝对路径）按 error 处理，
   不读取仓库外的 LICENSE。
5. **无工作树 clone**：discover 的 ``--no-checkout`` clone 从 git 对象库
   物化 license 文件（``local_tree``），避免误判为 missing。
6. **缓存新鲜度**：无 marker 的半成品远程缓存不被视为新鲜，避免把拉取
   中断的目录误判为 missing。
7. **discover 预检**：返回 license 类型/三态并按目录与声明复用结论；
   预检异常不写值、不缓存失败结果。

license 定时任务已移除，相关调度/落库/可见性用例不再保留。

这些用例不依赖真实数据库与网络：判定用本地临时目录，预检用
``SimpleNamespace`` 桩对象。
"""

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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


def test_with_extra_allow_exception_returns_unknown_not_keyerror(tmp_path):
    """WITH 后的例外令牌命中 extra_allow（不在准入清单）时，不应抛 KeyError。

    ``resolve`` 对未收录 token 会返回 ``extra_allow`` 中的值，而该值并不在
    ``_entries`` 里；WITH 分支若直接 ``self._entries[...]`` 下标会 KeyError。
    期望返回 unknown（非 allowed），而不是崩溃。
    """
    access_file = tmp_path / "access.yaml"
    access_file.write_text(ACCESS_LIST, encoding="utf-8")
    access = LicenseAccessList(
        source=str(access_file), extra_allow=["CANN-2.0"]
    ).load()

    verdict = access.evaluate("MIT WITH CANN-2.0")

    assert verdict.status == STATUS_UNKNOWN
    assert verdict_to_flag(verdict.status) == 0
    assert any("例外" in reason for reason in verdict.reasons)


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
    """预检异常不写入结论（等待下一轮重试），不缓存失败结果。"""

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

