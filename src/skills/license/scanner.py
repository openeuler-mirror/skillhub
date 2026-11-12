"""skill license 扫描器：定位仓库文件树并调用门禁同源判定引擎。

仓库优先使用 discover 维护的本地 clone（``skill_repos.local_path``）；
本地缺失时使用远程缓存目录，缓存过期或不存在时按需浅克隆，只拉取
license 文件，避免整仓下载。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from src.skills.license.gate_engine import (
    STATUS_ERROR,
    LicenseAccessList,
    LicenseScanner,
    LicenseVerdict,
)

logger = logging.getLogger(__name__)

_LICENSE_NAME_RE = re.compile(
    r"^(?:licen[cs]e|licence|copying|unlicense)(?:[-._].*)?$", re.IGNORECASE
)
_MARKER = ".license_fetched_at"


def normalize_repo_key(repo_url: str) -> str:
    """把仓库 URL 归一化为 ``host_owner_repo``，与 discover 的目录名一致。"""
    text = (repo_url or "").strip()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.removesuffix(".git").rstrip("/")
    return text.replace("/", "_")


def derive_skill_dir(source_url: str) -> str:
    """从 SKILL.md 的 URL 推导 skill 目录（相对仓库根）。"""
    if not source_url:
        return ""
    url = source_url.split("?")[0]
    patterns = (
        r"https?://(?:github\.com|gitcode\.com|atomgit\.com|gitee\.com|gitlab\.com)/"
        r"[^/]+/[^/]+/(?:blob|-/blob)/[^/]+/(.*)$",
        r"https?://raw\.githubusercontent\.com/[^/]+/[^/]+/[^/]+/(.*)$",
        r"https?://api\.atomgit\.com/api/v5/repos/[^/]+/[^/]+/raw/(.*)$",
    )
    for pattern in patterns:
        match = re.match(pattern, url)
        if match:
            return os.path.dirname(match.group(1)).strip("/")
    return ""


def is_protected_repo(repo_url: str, patterns: list[str]) -> bool:
    """repo_url 是否命中保护名单；支持 ``host/owner/*`` 前缀与 fnmatch 通配。"""
    if not repo_url or not patterns:
        return False
    normalized = (repo_url or "").strip()
    for prefix in ("https://", "http://"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    normalized = normalized.removesuffix(".git").rstrip("/")

    from fnmatch import fnmatch

    for raw in patterns:
        pattern = (raw or "").strip()
        for prefix in ("https://", "http://"):
            if pattern.startswith(prefix):
                pattern = pattern[len(prefix):]
        pattern = pattern.removesuffix(".git").rstrip("/")
        if not pattern:
            continue
        if pattern.endswith("/*"):
            if normalized.startswith(pattern[:-1]):
                return True
        elif fnmatch(normalized, pattern):
            return True
    return False


@dataclass
class LicenseCheckResult:
    """单个 skill 的判定结果与仓库来源。"""

    verdict: LicenseVerdict
    repo_source: str
    repo_root: str = ""


class LicenseChecker:
    """按门禁逻辑判定 skill license，并负责仓库文件树准备。"""

    def __init__(
        self,
        access: LicenseAccessList,
        *,
        prefer_file: bool = True,
        remote_cache_root: str = "",
        remote_cache_ttl_days: int = 7,
        remote_fetch: bool = True,
        git_timeout: float = 60.0,
    ):
        self._access = access
        self._scanner = LicenseScanner(access, prefer_file=prefer_file)
        self._remote_cache_root = Path(remote_cache_root).expanduser() if remote_cache_root else None
        self._remote_cache_ttl_days = max(0, int(remote_cache_ttl_days))
        self._remote_fetch = remote_fetch
        self._git_timeout = git_timeout

    @property
    def access(self) -> LicenseAccessList:
        return self._access

    def check(
        self,
        repo_url: str,
        local_path: str | None,
        skill_dir: str,
        declared: str | None,
    ) -> LicenseCheckResult:
        """判定一个 skill；仓库树按 本地 clone -> 远程缓存 -> 按需拉取 的顺序准备。"""
        root, source = self._resolve_repo_root(repo_url, local_path)
        if root is None:
            return LicenseCheckResult(
                LicenseVerdict(STATUS_ERROR, reasons=[f"仓库文件树不可用: {repo_url}"]),
                "error",
            )
        return self.check_local(root, skill_dir, declared, source=source)

    def check_local(
        self,
        repo_root: str | Path,
        skill_dir: str,
        declared: str | None,
        *,
        source: str = "local",
    ) -> LicenseCheckResult:
        root = Path(repo_root)
        target = root / skill_dir if skill_dir else root
        try:
            verdict = self._scanner.scan(root, target, declared)
        except Exception as exc:  # 判定异常不阻断任务，按 error 处理
            logger.warning("license scan failed: root=%s dir=%s", root, skill_dir, exc_info=True)
            verdict = LicenseVerdict(
                STATUS_ERROR, reasons=[f"license 检查内部错误: {str(exc)[:200]}"]
            )
        return LicenseCheckResult(verdict, source, str(root))

    # ------------------------------------------------------------------
    # 仓库文件树
    # ------------------------------------------------------------------

    def _resolve_repo_root(
        self, repo_url: str, local_path: str | None
    ) -> tuple[Path | None, str]:
        if local_path:
            path = Path(local_path).expanduser()
            if path.is_dir():
                return path, "local"
            logger.warning("local clone missing, will try remote: %s", path)

        if self._remote_cache_root is None:
            return (None, "error") if not self._remote_fetch else self._fetch_remote(repo_url)

        cache_dir = self._remote_cache_root / normalize_repo_key(repo_url)
        if cache_dir.is_dir() and self._cache_fresh(cache_dir):
            return cache_dir, "remote_cache"
        if not self._remote_fetch:
            return None, "error"
        return self._fetch_remote(repo_url, cache_dir)

    def _cache_fresh(self, path: Path) -> bool:
        if self._remote_cache_ttl_days <= 0:
            return path.is_dir()
        marker = path / _MARKER
        try:
            mtime = marker.stat().st_mtime if marker.is_file() else path.stat().st_mtime
        except OSError:
            return False
        return time.time() - mtime < self._remote_cache_ttl_days * 86400

    def _fetch_remote(
        self, repo_url: str, cache_dir: Path | None = None
    ) -> tuple[Path | None, str]:
        url = (repo_url or "").strip()
        if not url.startswith(("https://", "http://")):
            logger.warning("unsupported repo url for remote fetch: %s", repo_url)
            return None, "error"
        if cache_dir is None:
            if self._remote_cache_root is None:
                return None, "error"
            cache_dir = self._remote_cache_root / normalize_repo_key(url)

        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
        try:
            if self._fetch_license_tree(url, cache_dir):
                (cache_dir / _MARKER).write_text(str(int(time.time())), encoding="utf-8")
                logger.info("license tree fetched: %s", cache_dir)
                return cache_dir, "remote_fetch"
        except Exception:
            logger.warning("partial fetch failed, fallback to shallow clone: %s", url, exc_info=True)

        shutil.rmtree(cache_dir, ignore_errors=True)
        if self._shallow_clone(url, cache_dir):
            (cache_dir / _MARKER).write_text(str(int(time.time())), encoding="utf-8")
            logger.info("license repo cloned: %s", cache_dir)
            return cache_dir, "remote_fetch"
        shutil.rmtree(cache_dir, ignore_errors=True)
        return None, "error"

    def _fetch_license_tree(self, url: str, cache_dir: Path) -> bool:
        """只物化 license 文件的轻量拉取；失败返回 False 由调用方回退。"""
        cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="license-clone-") as tmp:
            if not self._git(["clone", "--depth", "1", "--filter=blob:none", "--no-checkout", "-q", url, tmp]):
                return False
            rc, out = self._git_output(["-C", tmp, "ls-tree", "-r", "HEAD"])
            if rc != 0:
                return False
            paths = []
            for line in out.splitlines():
                parts = line.split("\t", 1)
                if len(parts) != 2:
                    continue
                meta, path = parts
                if meta.startswith("120000"):
                    continue  # 符号链接不物化，避免把链接目标当作正文
                name = os.path.basename(path)
                if _LICENSE_NAME_RE.match(name) or "/LICENSES/" in path or path.startswith("LICENSES/"):
                    paths.append(path)
            for path in paths[:80]:
                rc, data = self._git_bytes(["-C", tmp, "show", f"HEAD:{path}"])
                if rc != 0 or not data:
                    continue
                dest = cache_dir / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
        return True

    def _shallow_clone(self, url: str, cache_dir: Path) -> bool:
        return self._git(["clone", "--depth", "1", "-q", url, str(cache_dir)])

    def _git(self, args: list[str]) -> bool:
        rc, _ = self._run(["git", *args])
        return rc == 0

    def _git_output(self, args: list[str]) -> tuple[int, str]:
        return self._run(["git", *args], text=True)

    def _git_bytes(self, args: list[str]) -> tuple[int, bytes]:
        return self._run(["git", *args])

    def _run(self, cmd: list[str], text: bool = False):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self._git_timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            return proc.returncode, proc.stdout.decode("utf-8", "ignore") if text else proc.stdout
        except subprocess.TimeoutExpired:
            logger.warning("git command timeout: %s", " ".join(cmd[:3]))
            return 124, "" if text else b""
        except OSError:
            logger.warning("git command failed: %s", " ".join(cmd[:3]), exc_info=True)
            return 1, "" if text else b""

    def prune_remote_cache(self, keep_days: int) -> int:
        """清理超过保留期的远程仓库缓存。"""
        if self._remote_cache_root is None or keep_days <= 0:
            return 0
        removed = 0
        cutoff = time.time() - keep_days * 86400
        try:
            entries = list(self._remote_cache_root.iterdir())
        except OSError:
            return 0
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed
