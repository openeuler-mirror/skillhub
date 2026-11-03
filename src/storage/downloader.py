import asyncio
import hashlib
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

import aiofiles

from src.core.config import get_settings
from src.models.orm import Skill, SkillRepoModel, SkillVersion
from src.skills.skill_id import slugify_identifier

settings = get_settings()
logger = logging.getLogger(__name__)
MAX_STORAGE_READ_BYTES = 20 * 1024 * 1024


class SkillArchiveError(Exception):
    """Base error raised while resolving or packaging a Skill archive."""


class SkillArchiveConflictError(SkillArchiveError):
    """Raised when indexed Skill data cannot be resolved from the local clone."""


class SkillArchiveNotFoundError(SkillArchiveError):
    """Raised when the indexed Skill tree is absent from the requested commit."""


@dataclass(frozen=True)
class SkillArchive:
    path: Path
    filename: str
    media_type: str = "application/zip"
    sha256: str | None = None


class DownloadManager:
    def __init__(self, storage_path: Path | None = None):
        self.storage_path = Path(storage_path or settings.storage.local_path).resolve()
        self.storage_path.mkdir(parents=True, exist_ok=True)

    async def create_skill_archive(
        self,
        *,
        skill: Skill | SkillVersion,
        repository: SkillRepoModel,
    ) -> SkillArchive:
        repository_path = self._validate_repository_path(repository.local_path)
        relative_path = self.resolve_skill_relative_path(
            skill_id=skill.skill_id,
            source=repository.source,
            repository_url=repository.url,
            source_url=skill.source_url,
            refs=[skill.commit_id, skill.version, repository.branch, 'HEAD', 'master', 'main'],
        )
        commit_id = self._validate_commit_id(skill.commit_id)

        await self._ensure_git_object_exists(
            repository_path,
            f"{commit_id}^{{commit}}",
            "Skill commit is not available in local repository",
        )
        await self._ensure_git_object_exists(
            repository_path,
            f"{commit_id}:{relative_path}/SKILL.md",
            "Skill files are not available at the indexed commit",
            not_found=True,
        )

        archive_path = await self._build_skill_archive(
            repository_path=repository_path,
            commit_id=commit_id,
            relative_path=relative_path,
            skill_name=skill.name,
            skill_id=skill.skill_id,
        )
        return SkillArchive(
            path=archive_path,
            filename=self._build_archive_filename(skill),
            sha256=await self._sha256_of(archive_path),
        )

    @staticmethod
    def resolve_skill_relative_path(
        *,
        skill_id: str,
        source: str,
        repository_url: str | None,
        source_url: str,
        refs: list[str],
    ) -> str:
        owner_repo = DownloadManager._extract_owner_repo(repository_url)
        prefix = f"{source}:{owner_repo}/"
        if not skill_id.startswith(prefix):
            raise SkillArchiveConflictError(f"Skill ID does not belong to repository: {skill_id}")

        # skill_id only carries the skill name now, so the repository-relative
        # directory is recovered from the browse URL.  The ref follows /blob/
        # and may itself contain slashes, so match against known refs instead
        # of blindly splitting on '/'.
        for ref in dict.fromkeys(ref for ref in refs if ref):
            marker = f"/blob/{ref}/"
            if marker not in source_url:
                continue
            file_path = source_url.split(marker, 1)[1].rstrip("/")
            if not file_path.endswith("SKILL.md"):
                continue
            relative_path = file_path.removesuffix("SKILL.md").rstrip("/")
            path = PurePosixPath(relative_path)
            if not relative_path or path.is_absolute() or ".." in path.parts:
                raise SkillArchiveConflictError("Invalid Skill repository path")
            return path.as_posix()

        raise SkillArchiveNotFoundError(
            "Skill relative path cannot be resolved from source URL"
        )

    @staticmethod
    def _extract_owner_repo(repository_url: str | None) -> str:
        if not repository_url:
            raise SkillArchiveConflictError("Skill repository URL is missing")

        normalized_url = repository_url.strip()
        ssh_match = re.match(r"git@[^:]+:(.+)", normalized_url)
        if ssh_match:
            repository_path = ssh_match.group(1).strip("/")
        else:
            parsed = urlparse(normalized_url)
            if not parsed.netloc:
                raise SkillArchiveConflictError("Invalid Skill repository URL")
            repository_path = parsed.path.strip("/")

        repository_path = repository_path.removesuffix(".git")
        segments = [unquote(segment) for segment in repository_path.split("/") if segment]
        if len(segments) < 2:
            raise SkillArchiveConflictError("Invalid Skill repository URL")

        owner = slugify_identifier(segments[-2])
        repository_name = slugify_identifier(segments[-1])
        if not owner or not repository_name:
            raise SkillArchiveConflictError("Invalid Skill repository URL")
        return f"{owner}/{repository_name}"

    @staticmethod
    def _validate_repository_path(local_path: str | None) -> Path:
        if not local_path:
            raise SkillArchiveConflictError("Skill repository local path is missing")

        repository_path = Path(local_path).expanduser().resolve()
        if not repository_path.is_dir():
            raise SkillArchiveConflictError("Local Skill repository does not exist")
        if not (repository_path / ".git").exists():
            raise SkillArchiveConflictError("Local Skill repository is not a Git repository")
        return repository_path

    @staticmethod
    def _validate_commit_id(commit_id: str | None) -> str:
        if not commit_id:
            raise SkillArchiveConflictError("Skill commit ID is missing")
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_id):
            raise SkillArchiveConflictError("Invalid Skill commit ID")
        return commit_id

    async def _ensure_git_object_exists(
        self,
        repository_path: Path,
        object_name: str,
        error_message: str,
        *,
        not_found: bool = False,
    ) -> None:
        return_code, _, _ = await self._run_git(
            repository_path,
            "cat-file",
            "-e",
            object_name,
        )
        if return_code == 0:
            return
        error_type = SkillArchiveNotFoundError if not_found else SkillArchiveConflictError
        raise error_type(error_message)

    async def _build_skill_archive(
        self,
        *,
        repository_path: Path,
        commit_id: str,
        relative_path: str,
        skill_name: str,
        skill_id: str,
    ) -> Path:
        cache_directory = self.storage_path / "download-cache"
        cache_directory.mkdir(parents=True, exist_ok=True)

        archive_key = hashlib.sha256(f"{skill_id}:{commit_id}:{relative_path}".encode()).hexdigest()
        archive_path = cache_directory / f"{archive_key}.zip"
        if archive_path.is_file() and archive_path.stat().st_size > 0:
            return archive_path

        temporary_path = cache_directory / f".{archive_key}.{uuid.uuid4().hex}.tmp"
        archive_root = self._sanitize_filename(skill_name)
        try:
            return_code, _, _ = await self._run_git(
                repository_path,
                "archive",
                "--format=zip",
                f"--prefix={archive_root}/",
                f"--output={temporary_path}",
                f"{commit_id}:{relative_path}",
            )
            if return_code != 0 or not temporary_path.is_file():
                raise SkillArchiveError("Failed to package Skill")
            os.replace(temporary_path, archive_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return archive_path

    @staticmethod
    async def _sha256_of(path: Path) -> str | None:
        """SHA-256 digest of a packaged archive, for client-side verification."""
        digest = hashlib.sha256()
        try:
            async with aiofiles.open(path, 'rb') as handle:
                while chunk := await handle.read(1024 * 1024):
                    digest.update(chunk)
        except OSError:
            logger.warning("Failed to hash Skill archive: %s", path, exc_info=True)
            return None
        return digest.hexdigest()

    @staticmethod
    async def _run_git(repository_path: Path, *args: str) -> tuple[int, bytes, bytes]:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repository_path),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return process.returncode or 0, stdout, stderr

    @staticmethod
    def _sanitize_filename(value: str) -> str:
        cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip(".-_")
        return cleaned or "skill"

    def _build_archive_filename(self, skill: Skill | SkillVersion) -> str:
        name = self._sanitize_filename(skill.name)
        if skill.version:
            version = self._sanitize_filename(skill.version)
            return f"{name}-{version}.zip"
        return f"{name}.zip"


class LocalStorage:
    def __init__(self, base_path: Path | None = None):
        self.base_path = Path(base_path or settings.storage.local_path).resolve()

    def _resolve_path(
        self,
        skill_id: str,
        filename: str | None = None,
    ) -> Path:
        if not skill_id or "\x00" in skill_id:
            raise ValueError("Invalid skill ID")
        if filename is not None and (not filename or "\x00" in filename):
            raise ValueError("Invalid filename")

        path = self.base_path / skill_id
        if filename is not None:
            path /= filename
        resolved_path = path.resolve()

        if (
            resolved_path == self.base_path
            or not resolved_path.is_relative_to(self.base_path)
        ):
            raise ValueError("Storage path escapes base directory")
        return resolved_path

    async def save(self, skill_id: str, content: bytes | str, filename: str | None = None) -> Path:
        file_path = self._resolve_path(skill_id, filename or "skill_data")
        file_path.parent.mkdir(parents=True, exist_ok=True)

        mode = "wb" if isinstance(content, bytes) else "w"
        async with aiofiles.open(file_path, mode) as f:
            await f.write(content)

        return file_path

    async def read(self, skill_id: str, filename: str) -> bytes | str | None:
        file_path = self._resolve_path(skill_id, filename)

        if not file_path.is_file():
            return None
        if file_path.stat().st_size > MAX_STORAGE_READ_BYTES:
            raise ValueError("File is too large")

        mode = "rb" if file_path.suffix in [".zip", ".tar", ".gz"] else "r"
        async with aiofiles.open(file_path, mode) as f:
            return await f.read()

    async def delete(self, skill_id: str) -> bool:
        skill_dir = self._resolve_path(skill_id)

        if skill_dir.is_dir():
            shutil.rmtree(skill_dir)
            return True
        return False

    async def list_files(self, skill_id: str) -> list[str]:
        skill_dir = self._resolve_path(skill_id)

        if not skill_dir.is_dir():
            return []

        return [f.name for f in skill_dir.iterdir() if f.is_file()]
