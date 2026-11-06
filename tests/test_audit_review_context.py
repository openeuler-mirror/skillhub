import os
import subprocess
from dataclasses import replace
from pathlib import Path

import httpx

from src.security.audit_review.context import (
    CONTEXT_VERSION,
    ContextConfig,
    FileContentCache,
    RemoteFetchBudget,
    ResolvedFile,
    build_repo_target,
    group_consecutive_by_file,
    new_context_stats,
    read_local_file,
    record_context,
    render_context,
    repo_url_from_source,
    resolve_file,
    select_file_batch,
    split_chunks,
)
from src.security.audit_review.remote_raw import RemoteTokens

REPO_URL = "https://github.com/owner/repo"
SOURCE_URL = "https://github.com/owner/repo/blob/main/skills/demo/SKILL.md"
SKILL_ID = "github:owner/repo/demo"


def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


def _make_repo(tmp_path: Path) -> tuple[str, Path]:
    repo = tmp_path / "github.com_owner_repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    target_file = repo / "skills" / "demo" / "SKILL.md"
    target_file.parent.mkdir(parents=True)
    target_file.write_text(
        "\n".join(f"line {index}" for index in range(1, 121)) + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return _git(repo, "rev-parse", "HEAD"), repo


def _target(tmp_path: Path, commit: str, cache_root: Path | None = None):
    return build_repo_target(
        repo_url=REPO_URL,
        source_url=SOURCE_URL,
        source="github",
        skill_id=SKILL_ID,
        commit=commit,
        cache_root=str(cache_root or tmp_path),
    )


def _issue(file: str, line: int, severity: str = "HIGH") -> dict:
    return {
        "id": "PE2",
        "severity": severity,
        "location": {"file": file, "start_line": line},
    }


def test_build_repo_target_resolves_skill_path(tmp_path):
    commit, repo = _make_repo(tmp_path)
    target = _target(tmp_path, commit)

    assert target is not None
    assert target.commit == commit
    assert target.skill_path == "skills/demo"
    assert target.cache_repo == repo


def test_read_local_file_at_commit(tmp_path):
    commit, _ = _make_repo(tmp_path)
    target = _target(tmp_path, commit)

    resolved = read_local_file(target, "SKILL.md", ContextConfig())

    assert resolved.source == "cache"
    assert resolved.content is not None
    assert "line 10" in resolved.content


def test_read_local_file_missing_file_falls_back(tmp_path):
    commit, _ = _make_repo(tmp_path)
    target = _target(tmp_path, commit)

    resolved = read_local_file(target, "missing.md", ContextConfig())

    assert resolved.content is None
    assert resolved.reason == "cache_file_missing"


def test_read_local_file_rejects_traversal(tmp_path):
    commit, _ = _make_repo(tmp_path)
    target = _target(tmp_path, commit)

    resolved = read_local_file(target, "../secret", ContextConfig())

    assert resolved.content is None
    assert resolved.reason == "invalid_path"


def test_render_context_full_for_small_file(tmp_path):
    commit, _ = _make_repo(tmp_path)
    target = _target(tmp_path, commit)
    resolved = read_local_file(target, "SKILL.md", ContextConfig())

    context = render_context(
        resolved,
        [_issue("SKILL.md", 10)],
        ContextConfig(),
        file="SKILL.md",
    )

    assert context.mode == "full"
    assert context.text is not None
    assert "10| line 10" in context.text


def test_render_context_window_for_large_file(tmp_path):
    commit, _ = _make_repo(tmp_path)
    target = _target(tmp_path, commit)
    resolved = read_local_file(target, "SKILL.md", ContextConfig())
    config = ContextConfig(
        full_file_max_bytes=10,
        window_lines=2,
        window_header_lines=2,
        max_context_chars=10_000,
    )

    context = render_context(
        resolved,
        [_issue("SKILL.md", 10), _issue("SKILL.md", 100)],
        config,
        file="SKILL.md",
    )

    assert context.mode == "window"
    assert context.text is not None
    assert "1| line 1" in context.text
    assert "8| line 8" in context.text
    assert "12| line 12" in context.text
    assert "98| line 98" in context.text
    assert "lines omitted" in context.text
    assert "50| line 50" not in context.text


def test_render_context_snippet_when_content_missing():
    context = render_context(
        ResolvedFile(None, "snippet", "repo_cache_missing"),
        [_issue("SKILL.md", 10)],
        ContextConfig(),
        file="SKILL.md",
    )

    assert context.mode == "snippet"
    assert context.text is None


async def test_resolve_file_uses_local_cache(tmp_path):
    commit, _ = _make_repo(tmp_path)
    target = _target(tmp_path, commit)
    cache = FileContentCache()

    async with httpx.AsyncClient() as client:
        resolved = await resolve_file(
            client,
            target,
            "SKILL.md",
            ContextConfig(remote_fetch_enabled=False),
            RemoteTokens(),
            RemoteFetchBudget(1),
            cache,
        )

    assert resolved.source == "cache"
    assert resolved.content is not None
    assert cache.get((target.cache_repo.as_posix(), commit, "skills/demo/SKILL.md")) is not None


async def test_resolve_file_cache_does_not_share_files_across_skills(tmp_path, monkeypatch):
    commit, _ = _make_repo(tmp_path)
    target = _target(tmp_path, commit)
    other = replace(target, skill_path="skills/other")
    cache = FileContentCache()

    def fake_read(repo_target, file, config):
        return ResolvedFile(f"content for {repo_target.skill_path}\n", "cache")

    monkeypatch.setattr("src.security.audit_review.context.read_local_file", fake_read)
    config = ContextConfig(remote_fetch_enabled=False)

    async with httpx.AsyncClient() as client:
        first = await resolve_file(
            client, target, "SKILL.md", config, RemoteTokens(), RemoteFetchBudget(1), cache
        )
        second = await resolve_file(
            client, other, "SKILL.md", config, RemoteTokens(), RemoteFetchBudget(1), cache
        )

    assert first.content == "content for skills/demo\n"
    assert second.content == "content for skills/other\n"


async def test_resolve_file_falls_back_to_remote(tmp_path):
    commit, _ = _make_repo(tmp_path)
    target = _target(tmp_path, commit, cache_root=tmp_path / "empty")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"remote content\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolved = await resolve_file(
            client,
            target,
            "SKILL.md",
            ContextConfig(remote_fetch_enabled=True),
            RemoteTokens(),
            RemoteFetchBudget(1),
        )

    assert resolved.source == "remote"
    assert resolved.content == "remote content\n"


async def test_resolve_file_falls_back_to_snippet_on_remote_error(tmp_path):
    commit, _ = _make_repo(tmp_path)
    target = _target(tmp_path, commit, cache_root=tmp_path / "empty")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolved = await resolve_file(
            client,
            target,
            "SKILL.md",
            ContextConfig(remote_fetch_enabled=True),
            RemoteTokens(),
            RemoteFetchBudget(1),
        )

    assert resolved.source == "snippet"
    assert resolved.content is None
    assert resolved.reason == "remote_http_404"


def test_remote_budget():
    budget = RemoteFetchBudget(1)
    assert budget.acquire() is True
    assert budget.acquire() is False


def test_select_file_batch_is_file_first_and_cursor_stable():
    issues = [
        _issue("b.md", 5, "HIGH"),
        _issue("a.md", 100, "LOW"),
        _issue("a.md", 10, "CRITICAL"),
    ]

    batch, cursor, remaining = select_file_batch(issues, 0, 2)

    assert batch == [2, 1]
    assert cursor == 2
    assert remaining == 1

    next_batch, next_cursor, next_remaining = select_file_batch(issues, cursor, 2)
    assert next_batch == [0]
    assert next_cursor == 3
    assert next_remaining == 0


def test_group_and_split_chunks():
    issues = [_issue("a.md", line) for line in (1, 2, 3)] + [_issue("b.md", 1)]
    groups = group_consecutive_by_file(issues, [0, 1, 2, 3])

    assert groups == [("a.md", [0, 1, 2]), ("b.md", [3])]
    assert split_chunks([0, 1, 2], 2) == [[0, 1], [2]]


def test_repo_url_from_source_handles_gitlab_blob():
    assert (
        repo_url_from_source("https://gitlab.com/group/repo/-/blob/main/SKILL.md")
        == "https://gitlab.com/group/repo"
    )


def test_context_stats():
    stats = new_context_stats(ContextConfig())
    record_context(
        stats,
        render_context(
            ResolvedFile("hello\n", "remote"),
            [_issue("a.md", 1)],
            ContextConfig(),
            file="a.md",
        ),
    )
    record_context(
        stats,
        render_context(
            ResolvedFile(None, "snippet", "too_large"),
            [_issue("a.md", 1)],
            ContextConfig(),
            file="a.md",
        ),
    )

    assert stats["context_version"] == CONTEXT_VERSION
    assert stats["context_blocks_full"] == 1
    assert stats["context_blocks_snippet"] == 1
    assert stats["context_sources_remote"] == 1
    assert stats["context_missing_reasons"] == {"too_large": 1}
