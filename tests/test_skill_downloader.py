import asyncio
import hashlib
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.storage.downloader import (
    DownloadManager,
    SkillArchiveConflictError,
    SkillArchiveNotFoundError,
)


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _create_git_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "tests@example.com")
    _git(repository, "config", "user.name", "WittyHub Tests")

    skill_directory = repository / "packages" / "skills" / "clean-code"
    references = skill_directory / "references"
    references.mkdir(parents=True)
    (skill_directory / "SKILL.md").write_text("# Clean Code\n", encoding="utf-8")
    (references / "guard-clause.md").write_text("# Guard Clause\n", encoding="utf-8")

    other_skill = repository / "packages" / "skills" / "other-skill"
    other_skill.mkdir(parents=True)
    (other_skill / "SKILL.md").write_text("# Other Skill\n", encoding="utf-8")

    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "add skills")
    return repository, _git(repository, "rev-parse", "HEAD")


def _skill(
    commit_id: str,
    *,
    skill_id: str | None = None,
    source_url: str | None = None,
):
    return SimpleNamespace(
        skill_id=skill_id or "atomgit:openeuler/opendesign-components/clean-code",
        name="clean-code",
        version="1.1.0",
        commit_id=commit_id,
        source_url=(
            source_url
            or "https://atomgit.com/openeuler/opendesign-components/blob/master/packages/skills/clean-code/SKILL.md"
        ),
    )


def _repository(repository: Path):
    return SimpleNamespace(
        source="atomgit",
        url="https://atomgit.com/openeuler/opendesign-components.git",
        local_path=str(repository),
        branch="master",
    )


def test_resolve_skill_relative_path_keeps_nested_directories():
    relative_path = DownloadManager.resolve_skill_relative_path(
        skill_id="atomgit:openeuler/opendesign-components/clean-code",
        source="atomgit",
        repository_url="https://atomgit.com/openeuler/opendesign-components",
        source_url="https://atomgit.com/openeuler/opendesign-components/blob/master/packages/skills/clean-code/SKILL.md",
        refs=["master"],
    )

    assert relative_path == "packages/skills/clean-code"


def test_resolve_skill_relative_path_rejects_repository_mismatch():
    with pytest.raises(SkillArchiveConflictError):
        DownloadManager.resolve_skill_relative_path(
            skill_id="atomgit:another/repository/clean-code",
            source="atomgit",
            repository_url="https://atomgit.com/openeuler/opendesign-components",
            source_url="https://atomgit.com/openeuler/opendesign-components/blob/master/packages/skills/clean-code/SKILL.md",
            refs=["master"],
        )


def test_resolve_skill_relative_path_rejects_parent_traversal():
    with pytest.raises(SkillArchiveConflictError):
        DownloadManager.resolve_skill_relative_path(
            skill_id="atomgit:openeuler/opendesign-components/clean-code",
            source="atomgit",
            repository_url="https://atomgit.com/openeuler/opendesign-components",
            source_url="https://atomgit.com/openeuler/opendesign-components/blob/master/../secrets/SKILL.md",
            refs=["master"],
        )


def test_create_skill_archive_exports_only_target_skill(tmp_path):
    repository, commit_id = _create_git_repository(tmp_path)
    manager = DownloadManager(storage_path=tmp_path / "storage")

    archive = asyncio.run(
        manager.create_skill_archive(
            skill=_skill(commit_id),
            repository=_repository(repository),
        )
    )

    assert archive.filename == "clean-code-1.1.0.zip"
    assert archive.media_type == "application/zip"
    assert archive.sha256 == hashlib.sha256(archive.path.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive.path) as packaged_skill:
        files = set(packaged_skill.namelist())
    assert "clean-code/SKILL.md" in files
    assert "clean-code/references/guard-clause.md" in files
    assert not any("other-skill" in name for name in files)
    assert not any(".git" in name for name in files)


def test_create_skill_archive_hash_is_stable_across_cache_hits(tmp_path):
    repository, commit_id = _create_git_repository(tmp_path)
    manager = DownloadManager(storage_path=tmp_path / "storage")

    first = asyncio.run(
        manager.create_skill_archive(
            skill=_skill(commit_id),
            repository=_repository(repository),
        )
    )
    second = asyncio.run(
        manager.create_skill_archive(
            skill=_skill(commit_id),
            repository=_repository(repository),
        )
    )

    assert first.sha256 is not None
    assert second.sha256 == first.sha256
    assert second.sha256 == hashlib.sha256(second.path.read_bytes()).hexdigest()


def test_create_skill_archive_reuses_commit_cache(tmp_path):
    repository, commit_id = _create_git_repository(tmp_path)
    manager = DownloadManager(storage_path=tmp_path / "storage")

    first = asyncio.run(
        manager.create_skill_archive(
            skill=_skill(commit_id),
            repository=_repository(repository),
        )
    )
    first_mtime = first.path.stat().st_mtime_ns
    second = asyncio.run(
        manager.create_skill_archive(
            skill=_skill(commit_id),
            repository=_repository(repository),
        )
    )

    assert second.path == first.path
    assert second.path.stat().st_mtime_ns == first_mtime


def test_create_skill_archive_uses_indexed_commit(tmp_path):
    repository, commit_id = _create_git_repository(tmp_path)
    skill_file = repository / "packages" / "skills" / "clean-code" / "SKILL.md"
    skill_file.write_text("# Changed after indexing\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "change skill")

    manager = DownloadManager(storage_path=tmp_path / "storage")
    archive = asyncio.run(
        manager.create_skill_archive(
            skill=_skill(commit_id),
            repository=_repository(repository),
        )
    )

    with zipfile.ZipFile(archive.path) as packaged_skill:
        content = packaged_skill.read("clean-code/SKILL.md").decode()
    assert content == "# Clean Code\n"


def test_create_skill_archive_reports_missing_skill_at_commit(tmp_path):
    repository, commit_id = _create_git_repository(tmp_path)
    manager = DownloadManager(storage_path=tmp_path / "storage")

    with pytest.raises(SkillArchiveNotFoundError):
        asyncio.run(
            manager.create_skill_archive(
                skill=_skill(
                    commit_id,
                    skill_id="atomgit:openeuler/opendesign-components/missing",
                    source_url="https://atomgit.com/openeuler/opendesign-components/blob/master/packages/skills/missing/SKILL.md",
                ),
                repository=_repository(repository),
            )
        )
