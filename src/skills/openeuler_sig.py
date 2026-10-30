"""Resolve openEuler repository SIG ownership from community metadata."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import yaml

from src.core.config import get_settings

_logger = logging.getLogger(__name__)
settings = get_settings()

COMMUNITY_REPO_URL = "https://atomgit.com/openeuler/community.git"
COMMUNITY_CACHE_DIRNAME = "openeuler-community"


def build_openeuler_repo_sig_mapping(
    cache_dir: Path | None = None,
) -> dict[str, str]:
    """Return repo_name -> sig_name from local cached openEuler community metadata."""
    community_dir = cache_dir or _default_cache_dir()
    _sync_community_repo(community_dir)
    return _load_sig_mapping(community_dir)


def repo_name_from_openeuler_repo_slug(repo_slug: str) -> str | None:
    """Convert openeuler/repo-name into the crawler repo_name format."""
    normalized = repo_slug.strip().removesuffix(".git")
    if not normalized.lower().startswith("openeuler/"):
        return None
    parts = normalized.split("/", 1)
    if len(parts) != 2 or not parts[1].strip():
        return None
    return f"atomgit.com_openeuler_{parts[1].strip()}"


def repo_name_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url.strip())
    path = parsed.path.strip("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) < 2:
        return None
    return f"{parsed.netloc}_{parts[-2]}_{parts[-1]}".replace("/", "_")


def _default_cache_dir() -> Path:
    return Path(settings.storage.local_path).expanduser().resolve() / COMMUNITY_CACHE_DIRNAME


def _sync_community_repo(community_dir: Path) -> None:
    community_dir.parent.mkdir(parents=True, exist_ok=True)
    if community_dir.is_dir() and (community_dir / ".git").exists():
        _run_git(["git", "-C", str(community_dir), "pull", "--ff-only"])
        return
    if community_dir.exists():
        shutil.rmtree(community_dir)
    _run_git(["git", "clone", "--depth", "1", COMMUNITY_REPO_URL, str(community_dir)])


def _run_git(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)


def _load_sig_mapping(community_dir: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for sig_info_file in sorted(community_dir.glob("sig/*/sig-info.yaml")):
        sig_name = sig_info_file.parent.name
        try:
            sig_info = yaml.safe_load(sig_info_file.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            _logger.warning("Failed to parse openEuler SIG metadata %s: %s", sig_info_file, exc)
            continue
        for repo_slug in _iter_openeuler_repo_slugs(sig_info):
            repo_name = repo_name_from_openeuler_repo_slug(repo_slug)
            if repo_name:
                mapping.setdefault(repo_name, sig_name)
    return mapping


def _iter_openeuler_repo_slugs(sig_info: dict[str, object]):
    repositories = sig_info.get("repositories") or []
    if not isinstance(repositories, list):
        return
    for group in repositories:
        repo_entries: list[object] = []
        if isinstance(group, dict):
            raw_entries = group.get("repo") or []
            repo_entries.extend(raw_entries if isinstance(raw_entries, list) else [raw_entries])
        elif isinstance(group, str):
            repo_entries.append(group)
        for entry in repo_entries:
            if not isinstance(entry, str):
                continue
            repo_slug = entry.strip().removesuffix(".git")
            if repo_slug.lower().startswith("openeuler/"):
                yield repo_slug
