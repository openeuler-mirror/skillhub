"""skill license 扫描器：定位仓库文件树并调用门禁同源判定引擎。

职责边界：
- 只负责“把仓库文件树准备好”并调用 ``LicenseScanner.scan``；
- 不做写库、不删除、不做合规动作，判定结果由调用方映射为三态。

仓库文件树优先级（``LicenseChecker._resolve_repo_root``）：
1. discover 维护的本地 clone（``skill_repos.local_path``）：
   - 有工作树：直接扫描（``local``）；
   - ``--no-checkout`` partial clone（只有 ``.git``）：从本地 git 对象库
     按 HEAD 物化 license 文件到本轮临时目录（``local_tree``）；
2. 远程缓存目录（``license-repos/<host_owner_repo>``），未过期直接复用；
3. 按需浅克隆：先尝试只物化 license 文件的轻量拉取，失败再回退完整浅克隆。

所有 git 命令都带超时与 ``GIT_TERMINAL_PROMPT=0``，避免任务被卡住。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
# ``_local`` 缓存目录名是本地仓库路径 sha1 的前 16 位十六进制。
# 回收时用该模式限定只处理本模块创建的目录，避免 remote_cache_root 误配
# 到共享目录时删掉 ``_local`` 下其他程序的旧数据。
_LOCAL_CACHE_NAME_RE = re.compile(r"^[0-9a-f]{16}$")


def _is_license_path(path: str) -> bool:
    """判断 git 树中的路径是否属于 license 文件。

    与 ``LicenseScanner._license_files_in`` 的规则保持一致：
    文件名匹配 LICENSE/COPYING 等，或位于 ``LICENSES/`` 目录（REUSE 布局）。
    """
    name = os.path.basename(path)
    return bool(
        _LICENSE_NAME_RE.match(name)
        or "/LICENSES/" in path
        or path.startswith("LICENSES/")
    )


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


@dataclass
class LicenseCheckResult:
    """单个 skill 的判定结果与仓库来源。

    :ivar verdict: 引擎判定（status/reasons/sources/expression）
    :ivar repo_source: 仓库文件树来源，取值 ``local`` / ``local_tree`` /
        ``remote_cache`` / ``remote_fetch`` / ``error``；``local_tree`` 表示
        本地 clone 没有工作树（discover 使用 ``--no-checkout``），已从本地
        git 对象库按 HEAD 物化 license 文件；``error`` 表示文件树不可用，
        调用方应保持数据库原值，不要写 0
    :ivar repo_root: 实际使用的仓库根目录（便于日志排查）
    """

    verdict: LicenseVerdict
    repo_source: str
    repo_root: str = ""


class LicenseChecker:
    """按门禁逻辑判定 skill license，并负责仓库文件树准备。

    一个实例应在**一轮任务**内复用：
    - 内部 ``LicenseScanner`` 持有目录级 license 文件缓存，复用可显著减少
      同一仓库多 skill 的重复目录盘点；
    - 但缓存不做文件系统变更监听，跨轮长期持有可能读到过期数据。

    本地 clone 兼容两种形态：
    - 有工作树（手工 clone / 测试）：直接扫描文件系统（``local``）；
    - discover 的 ``--no-checkout`` partial clone：工作树为空，从本地 git
      对象库按 HEAD 物化 license 文件到临时目录后再扫描（``local_tree``）。
      这样既不用为判定触发完整 checkout，也避免把“工作树为空”误判为
      missing（否则所有无 SKILL.md 声明的 skill 都会被写成 0）。

    线程安全：判定逻辑只读共享的准入清单与缓存字典，discover 通过
    ``asyncio.to_thread`` 并发调用是安全的（缓存命中/写入均为单次 dict
    操作，最坏情况是重复计算一次）。
    """

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
        # 本地 clone -> 物化目录缓存；临时根目录按需创建，任务结束时 close()
        self._local_trees: dict[str, Path] = {}
        self._local_tmp_root: Path | None = None
        self._local_seq = 0
        # 判定按仓库并发（asyncio.to_thread），物化目录的分配/缓存需加锁
        self._local_lock = threading.Lock()

    @property
    def access(self) -> LicenseAccessList:
        """已加载的准入清单（任务日志可读取 hash 做版本追踪）。"""
        return self._access

    def close(self) -> None:
        """清理本地 clone 物化出的临时目录（一轮任务结束时调用）。

        远程缓存目录属于跨轮复用资源，这里不删除。
        """
        with self._local_lock:
            if self._local_tmp_root is not None:
                shutil.rmtree(self._local_tmp_root, ignore_errors=True)
                self._local_tmp_root = None
                self._local_trees.clear()

    def check(
        self,
        repo_url: str,
        local_path: str | None,
        skill_dir: str,
        declared: Any,
    ) -> LicenseCheckResult:
        """判定一个 skill。

        仓库文件树按 本地 clone（含无工作树的 license 物化）-> 远程缓存 ->
        按需拉取 的顺序准备；全部不可用时返回 ``STATUS_ERROR``（调用方
        据此保持 NULL/原值）。
        """
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
        declared: Any,
        *,
        source: str = "local",
    ) -> LicenseCheckResult:
        """在已知的仓库文件树上判定；异常统一降级为 error，不向上抛出。

        安全边界：``skill_dir`` 由 ``source_url`` 推导而来，必须落在
        ``repo_root`` 内。若出现绝对路径、``..`` 或指向仓库外的符号链接，
        直接按 ``error`` 处理，避免把仓库外的 LICENSE 当成该 skill 的许可
        （调用方对 error 保持原值，不会误写 0）。

        这样单条 skill 的判定异常不会中断整轮任务，也不会把该行写成 0。
        """
        try:
            root = Path(repo_root).resolve()
            target = (root / skill_dir).resolve() if skill_dir else root
            # 解析后再做归属校验，符号链接指向仓库外同样会被拒绝
            target.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            logger.warning(
                "license scan rejected: skill dir escapes repo root: root=%s dir=%r",
                repo_root,
                skill_dir,
            )
            return LicenseCheckResult(
                LicenseVerdict(
                    STATUS_ERROR,
                    reasons=[f"skill 目录不在仓库内: {skill_dir}"],
                ),
                "error",
                str(repo_root),
            )

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
                if self._has_worktree(path):
                    return path, "local"
                # discover 使用 git clone --no-checkout --filter=blob:none，
                # 本地只有 .git，没有工作树文件。直接扫描会把所有
                # LICENSE 当成不存在（missing -> 0），因此先从本地 git
                # 对象库物化 license 文件，失败再回退远程。
                materialized = self._materialize_local_license_tree(path)
                if materialized is not None:
                    return materialized, "local_tree"
                logger.warning(
                    "local clone has no worktree and license files could not be "
                    "materialized, will try remote: %s",
                    path,
                )
            else:
                logger.warning("local clone missing, will try remote: %s", path)

        if self._remote_cache_root is None:
            return (None, "error") if not self._remote_fetch else self._fetch_remote(repo_url)

        cache_dir = self._remote_cache_root / normalize_repo_key(repo_url)
        if cache_dir.is_dir() and self._cache_fresh(cache_dir):
            return cache_dir, "remote_cache"
        if not self._remote_fetch:
            return None, "error"
        return self._fetch_remote(repo_url, cache_dir)

    @staticmethod
    def _has_worktree(path: Path) -> bool:
        """判断本地 clone 是否有工作树文件。

        - ``--no-checkout`` 的 clone 目录只有 ``.git``：无工作树；
        - bare 仓库（``HEAD`` / ``objects`` / ``refs`` 且无 ``.git``）：无工作树；
        - 其他非 git 目录（手工准备的文件树）：视为有工作树，直接扫描。
        """
        try:
            if (path / ".git").exists():
                return any(entry.name != ".git" for entry in path.iterdir())
            if (
                (path / "HEAD").is_file()
                and (path / "objects").is_dir()
                and (path / "refs").is_dir()
            ):
                return False
            return any(path.iterdir())
        except OSError:
            return False

    def _materialize_local_license_tree(self, repo_path: Path) -> Path | None:
        """从本地 clone 的 git 对象库按 HEAD 物化 license 文件。

        只执行 ``ls-tree`` + 少量 ``git show``，不 checkout 整个仓库；
        物化结果优先缓存在 ``<remote_cache_root>/_local/<path-hash>``，用
        marker 记录 HEAD，HEAD 未变时跨轮复用；没有远程缓存根（测试/极简
        配置）时退化到本 checker 的临时目录（``close`` 清理）。同一轮内
        同一仓库只做一次。blob 缺失时 Git 会按需从 promisor remote 懒拉取；
        失败则由调用方回退远程 license 缓存/浅克隆。
        """
        try:
            key = str(repo_path.resolve())
        except OSError:
            return None
        with self._local_lock:
            cached = self._local_trees.get(key)
        if cached is not None:
            return cached

        rc, head_out = self._git_output(["-C", str(repo_path), "rev-parse", "HEAD"])
        head = head_out.strip().splitlines()[0] if rc == 0 and head_out.strip() else ""
        if not head:
            logger.warning("local clone has no readable HEAD: %s", repo_path)
            return None

        # 持久化复用：license 文件按仓库路径哈希 + HEAD 标记缓存在
        # remote_cache_root/_local 下；HEAD 未变时下一轮直接复用，避免
        # 每天重复触发 partial clone 的 blob 懒拉取。
        cache_dir: Path | None = None
        if self._remote_cache_root is not None:
            try:
                base = self._remote_cache_root / "_local"
                base.mkdir(parents=True, exist_ok=True)
                cache_dir = base / hashlib.sha1(
                    str(repo_path.resolve()).encode("utf-8")
                ).hexdigest()[:16]
            except OSError:
                cache_dir = None

        if cache_dir is not None:
            try:
                marker = cache_dir / _MARKER
                if marker.is_file():
                    try:
                        if marker.read_text(encoding="utf-8").strip() == head:
                            with self._local_lock:
                                self._local_trees[key] = cache_dir
                            return cache_dir
                    except OSError:
                        pass
                shutil.rmtree(cache_dir, ignore_errors=True)
                cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.warning("persistent license cache unavailable: %s", cache_dir)
                cache_dir = None

        if cache_dir is None:
            # 没有远程缓存根（测试/极简配置）或持久化目录不可写：
            # 退化为本轮临时目录。
            try:
                with self._local_lock:
                    if self._local_tmp_root is None:
                        self._local_tmp_root = Path(tempfile.mkdtemp(prefix="license-local-"))
                    cache_dir = self._local_tmp_root / f"repo-{self._local_seq}"
                    self._local_seq += 1
                cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.warning("temporary license cache unavailable", exc_info=True)
                return None

        rc, listing = self._git_output(["-C", str(repo_path), "ls-tree", "-r", head])
        if rc != 0:
            logger.warning("local clone ls-tree failed: %s", repo_path)
            return None
        paths = []
        for line in listing.splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            meta, path = parts
            if meta.startswith(("120000", "160000")):
                continue  # 符号链接/子模块不物化，避免把链接目标当作正文
            if _is_license_path(path):
                paths.append(path)

        fetched = 0
        for path in paths[:80]:
            rc, data = self._git_bytes(["-C", str(repo_path), "show", f"{head}:{path}"])
            if rc != 0 or not data:
                continue
            dest = cache_dir / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            fetched += 1

        # 有 license 路径但没有全部取到：多半是 blob 懒拉取失败，
        # 返回 None 让调用方回退远程缓存，避免误判为 missing。
        if paths and fetched < min(len(paths), 80):
            shutil.rmtree(cache_dir, ignore_errors=True)
            return None

        (cache_dir / _MARKER).write_text(head, encoding="utf-8")
        with self._local_lock:
            self._local_trees[key] = cache_dir
        logger.info(
            "local license tree materialized: repo=%s head=%s files=%d",
            repo_path,
            head[:12],
            fetched,
        )
        return cache_dir

    def _cache_fresh(self, path: Path) -> bool:
        """判断远程缓存目录是否可直接复用。

        只要有 marker 才算完整缓存：``_fetch_remote`` 成功写完文件后才写
        marker，拉取中断留下的半成品目录没有 marker。若按目录 mtime 判定
        为新鲜，半成品会被直接扫描并可能把整个仓库误判为 ``missing``；
        同时这类目录也不符合 ``prune_remote_cache`` 的回收条件，会永久残留。
        因此没有 marker 一律视为不新鲜，由调用方重新拉取（拉取前会清空目录）。
        """
        marker = path / _MARKER
        try:
            if not marker.is_file():
                return False
            if self._remote_cache_ttl_days <= 0:
                # 配置为 0 表示不做时间过期，但完整缓存（marker）仍必须存在
                return True
            mtime = marker.stat().st_mtime
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
                if meta.startswith(("120000", "160000")):
                    continue  # 符号链接/子模块不物化，避免把链接目标当作正文
                if _is_license_path(path):
                    paths.append(path)
            fetched = 0
            for path in paths[:80]:
                rc, data = self._git_bytes(["-C", tmp, "show", f"HEAD:{path}"])
                if rc != 0 or not data:
                    continue
                dest = cache_dir / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                fetched += 1
            # 与本地 clone 物化保持同一安全边界：只取到部分 license 文件时
            # 不能落缓存并标成“完整”，否则会把 incomplete 文件树当成
            # missing/unknown 写入 0；返回 False 让调用方回退完整浅克隆。
            expected = min(len(paths), 80)
            if paths and fetched < expected:
                logger.warning(
                    "partial license tree incomplete: url=%s expected=%d fetched=%d",
                    url,
                    expected,
                    fetched,
                )
                return False
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

    def prune_remote_cache(
        self,
        keep_days: int,
        active_keys: set[str] | None = None,
    ) -> int:
        """清理本轮未使用且超过保留期的远程仓库缓存。

        :param keep_days: 保留天数；``<= 0`` 表示禁用清理
        :param active_keys: 本轮仍在扫描的 ``normalize_repo_key`` 集合。
            命中的目录即使 mtime 很旧也保留，避免删掉仍在使用的缓存。
        """
        if self._remote_cache_root is None or keep_days <= 0:
            return 0
        removed = 0
        cutoff = time.time() - keep_days * 86400
        try:
            entries = list(self._remote_cache_root.iterdir())
        except OSError:
            return 0
        for entry in entries:
            # 不跟随/删除软链接，避免 remote_cache_root 中混入链接时误操作
            if entry.is_symlink() or not entry.is_dir():
                continue
            # _local 是本地 clone 的 license 物化缓存，由 prune_local_cache
            # 按自己的 active 集合单独清理。
            if entry.name == "_local":
                continue
            if active_keys is not None and entry.name in active_keys:
                continue
            try:
                marker = entry / _MARKER
                # 只回收本模块创建的缓存目录：没有 marker 的目录可能属于
                # 其他数据（remote_cache_root 误配到共享目录），一律不删。
                if not marker.is_file():
                    continue
                mtime = marker.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed

    def prune_local_cache(self, keep_days: int) -> int:
        """清理本轮未使用且超过保留期的 ``_local`` 物化缓存。

        ``_local`` 的目录名是本地仓库路径哈希，无法从远程 URL 反推，因此
        用本轮实际物化过的目录（``_local_trees``）作为 active 集合：
        本轮用到的目录无论 mtime 多旧都不删；仓库从配置移除、local_path
        变化后遗留的旧目录才会在超过保留期后被回收。

        只处理「目录名是 16 位 sha1 前缀 + 含 marker」的本模块目录：
        - 没有 marker 的目录可能是物化中断的残留或其他程序的数据，
          ``_materialize_local_license_tree`` 下次会自行重建，无需在此删除；
        - 名字不符合规则的目录一律跳过，防止 ``remote_cache_root`` 误配到
          共享目录时误删 ``_local`` 下的无关数据。
        """
        if self._remote_cache_root is None or keep_days <= 0:
            return 0
        local_root = self._remote_cache_root / "_local"
        try:
            entries = list(local_root.iterdir())
        except OSError:
            return 0

        active: set[str] = set()
        with self._local_lock:
            for path in self._local_trees.values():
                try:
                    active.add(str(path.resolve()))
                except (OSError, RuntimeError):
                    continue

        removed = 0
        cutoff = time.time() - keep_days * 86400
        for entry in entries:
            # 与远程回收一致：跳过软链接；目录名必须是本模块的 sha1 前缀，
            # 否则可能是误放在 _local 下的其他数据，不能删。
            if entry.is_symlink() or not entry.is_dir():
                continue
            if not _LOCAL_CACHE_NAME_RE.match(entry.name):
                continue
            try:
                if str(entry.resolve()) in active:
                    continue
                marker = entry / _MARKER
                # 物化成功时一定会写 marker；没有 marker 的目录不是完整缓存，
                # 不按本模块缓存处理（下次物化时会自行重建）。
                if not marker.is_file():
                    continue
                mtime = marker.stat().st_mtime
            except (OSError, RuntimeError):
                continue
            if mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed
