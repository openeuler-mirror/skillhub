"""Derive the skill directory (relative to its git repo) from its URLs."""

from __future__ import annotations

import re

from src.skills.skill_id import extract_owner_repo

# GitHub/GitCode use ``/blob/<ref>/...``; GitLab uses ``/-/blob/<ref>/...``.
_BLOB_RE = re.compile(r"/(?:-/)?blob/[^/]+/(.+)")


def derive_skill_path(
    source_url: str | None,
    source: str | None,
    skill_id: str | None,
    repo_url: str | None = None,
) -> str:
    """Return the skill directory inside its repository.

    An empty string means the repository root (for example a repo that only
    contains a root-level ``SKILL.md``).  The URL form is preferred; the
    ``skill_id`` fallback mirrors the historical behaviour in the skills
    route so manual audits and crawler audits scan the same directory.
    """
    text = (source_url or "").strip()
    blob_match = _BLOB_RE.search(text)
    if blob_match:
        relative = blob_match.group(1)
        if relative.endswith("SKILL.md"):
            return relative.rsplit("/", 1)[0] if "/" in relative else ""
        return ""

    try:
        owner_repo = extract_owner_repo(repo_url or source_url)
    except ValueError:
        return ""
    prefix = f"{(source or '').strip().lower()}/{owner_repo}"
    if skill_id == prefix:
        return ""
    if skill_id and skill_id.startswith(prefix + "/"):
        skill_path = skill_id.removeprefix(prefix + "/").strip("/")
        repository_name = owner_repo.rsplit("/", 1)[-1]
        if not skill_path or skill_path == repository_name:
            return ""
        return skill_path
    return ""
