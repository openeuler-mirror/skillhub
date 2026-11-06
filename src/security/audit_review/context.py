"""Build review context for one audit: local repo cache, remote raw, or snippet.

The SkillSpector report stored in ``security_audits.details`` only carries the
static findings and their short ``code_snippet``.  This module resolves the
file that each finding points at, in this order:

1. the shared local repository cache (``/opt/wittyhub/skill-repositories``),
   read at the exact audited commit with read-only git commands;
2. the platform's raw-file endpoint (GitHub / GitCode / GitLab / Gitee);
3. the stored snippet, when neither is available.

File content is rendered either as the full file (small files) or as merged
line windows around the findings, with a hard character budget.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from src.security.audit_review.remote_raw import (
    RawFetchError,
    RemoteTokens,
    RepoRef,
    fetch_raw_file,
    is_binary,
    normalise_repo_path,
    parse_repo_url,
)
from src.utils.skill_path import derive_skill_path

__all__ = [
    "CONTEXT_VERSION",
    "ContextConfig",
    "FileContentCache",
    "FileContext",
    "RemoteFetchBudget",
    "RepoTarget",
    "ResolvedFile",
    "build_repo_target",
    "fetch_remote_file",
    "group_consecutive_by_file",
    "issue_file",
    "issue_line",
    "new_context_stats",
    "ordered_issue_indices",
    "read_local_file",
    "record_context",
    "render_context",
    "repo_url_from_source",
    "resolve_file",
    "select_file_batch",
    "split_chunks",
]

#: Bump when the batching/context layout changes so old partial reviews restart.
CONTEXT_VERSION = 2

SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
_DEFAULT_RANK = 4
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


@dataclass(frozen=True)
class ContextConfig:
    mode: str = "window"  # snippet | window | full
    window_lines: int = 40
    window_header_lines: int = 40
    window_max_ranges: int = 24
    window_gap: int = 3
    full_file_max_bytes: int = 64 * 1024
    max_file_bytes: int = 512 * 1024
    max_context_chars: int = 60_000
    repo_cache_root: str = "/opt/wittyhub/skill-repositories"
    git_timeout: float = 20.0
    remote_fetch_enabled: bool = True
    remote_fetch_timeout: float = 15.0
    remote_fetch_max_bytes: int = 512 * 1024
    remote_fetch_max_files_per_audit: int = 40


@dataclass(frozen=True)
class RepoTarget:
    repo_url: str
    source_url: str
    skill_path: str
    commit: str
    ref: RepoRef
    cache_repo: Path | None


@dataclass
class ResolvedFile:
    content: str | None
    source: str  # cache | remote | snippet
    reason: str = ""
    size: int = 0


@dataclass
class FileContext:
    file: str
    text: str | None
    mode: str  # full | window | snippet
    source: str
    reason: str = ""


class RemoteFetchBudget:
    """Cap remote file downloads per audit; ``limit <= 0`` means unlimited."""

    def __init__(self, limit: int):
        self._remaining = None if limit <= 0 else limit

    def acquire(self) -> bool:
        if self._remaining is None:
            return True
        if self._remaining <= 0:
            return False
        self._remaining -= 1
        return True


class FileContentCache:
    """Small LRU cache keyed by ``(repo, commit, path)`` with a char budget."""

    def __init__(self, max_chars: int = 64 * 1024 * 1024):
        self._max_chars = max(0, max_chars)
        self._chars = 0
        self._items: OrderedDict[tuple[str, str, str], str] = OrderedDict()

    def get(self, key: tuple[str, str, str]) -> str | None:
        value = self._items.get(key)
        if value is None:
            return None
        self._items.move_to_end(key)
        return value

    def put(self, key: tuple[str, str, str], value: str) -> None:
        if not value or len(value) > self._max_chars:
            return
        old = self._items.pop(key, None)
        if old is not None:
            self._chars -= len(old)
        self._items[key] = value
        self._chars += len(value)
        while self._chars > self._max_chars and self._items:
            _, evicted = self._items.popitem(last=False)
            self._chars -= len(evicted)


def repo_url_from_source(source_url: str | None) -> str:
    """Return the repository root for a ``.../blob/<ref>/...`` URL."""
    text = (source_url or "").strip()
    for marker in ("/-/blob/", "/blob/"):
        if marker in text:
            return text.split(marker, 1)[0]
    return text


def repo_cache_key(ref: RepoRef) -> str:
    """Match the cache directory layout used by ``checkout.sh``."""
    return f"{ref.host}_{ref.full_path.replace('/', '_')}"


def build_repo_target(
    *,
    repo_url: str | None,
    source_url: str | None,
    source: str | None,
    skill_id: str | None,
    commit: str | None,
    cache_root: str,
) -> RepoTarget | None:
    """Resolve the repository, audited commit and skill directory."""
    commit_text = (commit or "").strip()
    if not _COMMIT_RE.fullmatch(commit_text):
        return None
    root = repo_url_from_source(repo_url) if repo_url else ""
    if not root:
        root = repo_url_from_source(source_url)
    ref = parse_repo_url(root)
    if ref is None:
        return None
    cache_repo = Path(cache_root) / repo_cache_key(ref)
    if not (cache_repo / ".git").is_dir():
        cache_repo = None
    return RepoTarget(
        repo_url=root,
        source_url=source_url or "",
        skill_path=derive_skill_path(
            source_url=source_url,
            source=source,
            skill_id=skill_id,
            repo_url=root,
        ),
        commit=commit_text,
        ref=ref,
        cache_repo=cache_repo,
    )


def _repo_file_path(skill_path: str, file: str) -> str:
    file_path = normalise_repo_path(file)
    prefix = normalise_repo_path(skill_path) if (skill_path or "").strip() else ""
    return f"{prefix}/{file_path}" if prefix else file_path


def _git(repo: Path, args: list[str], timeout: float) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
        capture_output=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def read_local_file(target: RepoTarget, file: str, config: ContextConfig) -> ResolvedFile:
    """Read ``commit:skill_path/file`` from the shared local cache."""
    if target.cache_repo is None:
        return ResolvedFile(None, "snippet", "repo_cache_missing")
    try:
        repo_path = _repo_file_path(target.skill_path, file)
    except RawFetchError as exc:
        return ResolvedFile(None, "snippet", exc.reason)

    ref = f"{target.commit}:{repo_path}"
    try:
        stat = _git(target.cache_repo, ["cat-file", "-s", ref], config.git_timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        reason = "git_timeout" if isinstance(exc, subprocess.TimeoutExpired) else "git_error"
        return ResolvedFile(None, "snippet", reason)
    if stat.returncode != 0:
        return ResolvedFile(None, "snippet", "cache_file_missing")
    try:
        size = int(stat.stdout.strip())
    except ValueError:
        size = 0
    if size <= 0:
        return ResolvedFile(None, "snippet", "empty")
    if size > config.max_file_bytes:
        return ResolvedFile(None, "snippet", "too_large", size=size)

    try:
        shown = _git(target.cache_repo, ["show", ref], config.git_timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        reason = "git_timeout" if isinstance(exc, subprocess.TimeoutExpired) else "git_error"
        return ResolvedFile(None, "snippet", reason)
    if shown.returncode != 0 or not shown.stdout:
        return ResolvedFile(None, "snippet", "cache_read_failed")
    data = shown.stdout
    if is_binary(data):
        return ResolvedFile(None, "snippet", "binary", size=size)
    try:
        return ResolvedFile(data.decode("utf-8-sig"), "cache", size=size)
    except UnicodeDecodeError:
        return ResolvedFile(None, "snippet", "binary", size=size)


async def fetch_remote_file(
    client: httpx.AsyncClient,
    target: RepoTarget,
    file: str,
    config: ContextConfig,
    tokens: RemoteTokens,
    budget: RemoteFetchBudget,
) -> ResolvedFile:
    """Fetch ``commit:skill_path/file`` from the platform's raw endpoint."""
    if not config.remote_fetch_enabled:
        return ResolvedFile(None, "snippet", "remote_disabled")
    if not budget.acquire():
        return ResolvedFile(None, "snippet", "remote_budget_exhausted")
    try:
        repo_path = _repo_file_path(target.skill_path, file)
    except RawFetchError as exc:
        return ResolvedFile(None, "snippet", exc.reason)
    try:
        text = await fetch_raw_file(
            client,
            target.ref,
            target.commit,
            repo_path,
            tokens=tokens,
            timeout=config.remote_fetch_timeout,
            max_bytes=config.remote_fetch_max_bytes,
        )
    except RawFetchError as exc:
        return ResolvedFile(None, "snippet", f"remote_{exc.reason}")
    except Exception:
        return ResolvedFile(None, "snippet", "remote_error")
    return ResolvedFile(text, "remote", size=len(text))


async def resolve_file(
    client: httpx.AsyncClient,
    target: RepoTarget | None,
    file: str,
    config: ContextConfig,
    tokens: RemoteTokens,
    budget: RemoteFetchBudget,
    cache: FileContentCache | None = None,
) -> ResolvedFile:
    """Resolve one file with cache -> remote -> snippet fallback."""
    if target is None:
        return ResolvedFile(None, "snippet", "no_repo_context")
    # 同一仓库/commit 下不同 skill 目录可能都有同名文件（如 SKILL.md），
    # 缓存键必须带上 skill_path，否则复核会拿到别的 skill 的文件内容。
    file_key = f"{target.skill_path}/{file}" if target.skill_path else file
    cache_key = (
        target.cache_repo.as_posix() if target.cache_repo else target.repo_url,
        target.commit,
        file_key,
    )
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            return ResolvedFile(cached, "cache")

    local = await asyncio.to_thread(read_local_file, target, file, config)
    if local.content is not None:
        if cache is not None:
            cache.put(cache_key, local.content)
        return local

    remote = await fetch_remote_file(client, target, file, config, tokens, budget)
    if remote.content is not None:
        if cache is not None:
            cache.put(cache_key, remote.content)
        return remote
    # 远端未启用时保留本地失败原因：只返回 remote_disabled 会掩盖真正的
    # 缓存缺失/路径错误，误导排障；远端实际尝试过的失败仍按原样返回。
    if (
        remote.reason == "remote_disabled"
        and local.reason
        and local.reason != remote.reason
    ):
        return ResolvedFile(None, "snippet", f"{local.reason}+{remote.reason}")
    return remote


def issue_file(issue: Any) -> str:
    location = issue.get("location") if isinstance(issue, dict) else None
    if not isinstance(location, dict):
        return "unknown"
    return str(location.get("file") or "unknown")


def _issue_line(issue: Any, key: str) -> int:
    location = issue.get("location") if isinstance(issue, dict) else None
    if not isinstance(location, dict):
        return 0
    value = location.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def issue_line(issue: Any) -> int:
    return _issue_line(issue, "start_line")


def issue_end_line(issue: Any) -> int:
    return _issue_line(issue, "end_line")


def issue_severity_rank(issue: Any) -> int:
    severity = issue.get("severity") if isinstance(issue, dict) else None
    return SEVERITY_RANK.get(str(severity or "").strip().upper(), _DEFAULT_RANK)


def ordered_issue_indices(issues: list[Any]) -> list[int]:
    """File-first, severity-second order used by the per-audit cursor."""
    groups: dict[str, list[int]] = {}
    for index, issue in enumerate(issues):
        groups.setdefault(issue_file(issue), []).append(index)
    files = sorted(
        groups,
        key=lambda name: (
            min(issue_severity_rank(issues[index]) for index in groups[name]),
            name,
        ),
    )
    ordered: list[int] = []
    for name in files:
        ordered.extend(
            sorted(
                groups[name],
                key=lambda index: (
                    issue_severity_rank(issues[index]),
                    issue_line(issues[index]),
                    index,
                ),
            )
        )
    return ordered


def select_file_batch(
    issues: list[Any],
    reviewed_count: int,
    limit: int,
) -> tuple[list[int], int, int]:
    """Return the next file-grouped batch and the updated review cursor."""
    ordered = ordered_issue_indices(issues)
    start = max(0, min(reviewed_count, len(ordered)))
    end = len(ordered) if limit <= 0 else min(len(ordered), start + limit)
    return ordered[start:end], end, len(ordered) - end


def group_consecutive_by_file(
    issues: list[Any],
    indices: list[int],
) -> list[tuple[str, list[int]]]:
    groups: list[tuple[str, list[int]]] = []
    for index in indices:
        name = issue_file(issues[index])
        if groups and groups[-1][0] == name:
            groups[-1][1].append(index)
        else:
            groups.append((name, [index]))
    return groups


def split_chunks(indices: list[int], size: int) -> list[list[int]]:
    size = max(1, size)
    return [indices[offset : offset + size] for offset in range(0, len(indices), size)]


def _render_full(content: str) -> str:
    lines = content.splitlines()
    width = len(str(len(lines))) if lines else 1
    return "\n".join(f"{index:>{width}}| {line}" for index, line in enumerate(lines, 1))


def _merge_ranges(ranges: list[tuple[int, int]], gap: int) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + max(0, gap):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _candidate_ranges(
    findings: list[Any],
    line_count: int,
    config: ContextConfig,
) -> list[tuple[int, int, int]]:
    candidates: dict[tuple[int, int], int] = {}
    for finding in findings:
        start = issue_line(finding) or 1
        end = issue_end_line(finding) or start
        if end < start:
            end = start
        start = max(1, start - max(0, config.window_lines))
        end = min(line_count, end + max(0, config.window_lines))
        key = (start, end)
        rank = issue_severity_rank(finding)
        candidates[key] = min(candidates.get(key, _DEFAULT_RANK), rank)
    if config.window_header_lines > 0:
        header = (1, min(line_count, max(1, config.window_header_lines)))
        candidates[header] = min(candidates.get(header, _DEFAULT_RANK), -1)
    ranked = sorted(candidates.items(), key=lambda item: (item[1], item[0][0]))
    return [(start, end, rank) for (start, end), rank in ranked[: max(1, config.window_max_ranges)]]


def _render_ranges(lines: list[str], ranges: list[tuple[int, int]]) -> str:
    width = len(str(len(lines))) if lines else 1
    output: list[str] = []
    previous_end = 0
    for start, end in ranges:
        if start > previous_end + 1:
            output.append(f"... ({start - previous_end - 1} lines omitted) ...")
        for line_number in range(start, end + 1):
            output.append(f"{line_number:>{width}}| {lines[line_number - 1]}")
        previous_end = max(previous_end, end)
    return "\n".join(output)


def _render_window(
    content: str,
    findings: list[Any],
    config: ContextConfig,
) -> str | None:
    lines = content.splitlines()
    if not lines:
        return None
    candidates = _candidate_ranges(findings, len(lines), config)
    while candidates:
        ranges = _merge_ranges(
            [(start, end) for start, end, _ in candidates],
            config.window_gap,
        )
        text = _render_ranges(lines, ranges)
        if len(text) <= config.max_context_chars:
            return text
        # Candidates are sorted by severity, least severe last: drop it and retry.
        candidates.pop()
    return None


def render_context(
    resolved: ResolvedFile,
    findings: list[Any],
    config: ContextConfig,
    *,
    file: str,
) -> FileContext:
    """Render file content as full text, line windows, or a snippet fallback."""
    if resolved.content is None:
        return FileContext(file, None, "snippet", resolved.source, resolved.reason)
    if config.mode == "snippet":
        return FileContext(file, None, "snippet", resolved.source, "context_mode_snippet")

    content = resolved.content
    if config.mode in {"full", "window"}:
        if len(content.encode("utf-8")) <= config.full_file_max_bytes:
            text = _render_full(content)
            if len(text) <= config.max_context_chars:
                return FileContext(file, text, "full", resolved.source)
    text = _render_window(content, findings, config)
    if text is None:
        return FileContext(file, None, "snippet", resolved.source, "context_too_large")
    return FileContext(file, text, "window", resolved.source)


def new_context_stats(config: ContextConfig) -> dict[str, Any]:
    return {
        "context_version": CONTEXT_VERSION,
        "context_mode": config.mode,
        "context_blocks_full": 0,
        "context_blocks_window": 0,
        "context_blocks_snippet": 0,
        "context_sources_cache": 0,
        "context_sources_remote": 0,
        "context_missing_reasons": {},
    }


def record_context(stats: dict[str, Any], context: FileContext) -> None:
    key = f"context_blocks_{context.mode}"
    stats[key] = int(stats.get(key, 0)) + 1
    if context.source in {"cache", "remote"}:
        source_key = f"context_sources_{context.source}"
        stats[source_key] = int(stats.get(source_key, 0)) + 1
    if context.mode == "snippet" and context.reason:
        reasons = stats.setdefault("context_missing_reasons", {})
        reasons[context.reason] = int(reasons.get(context.reason, 0)) + 1
