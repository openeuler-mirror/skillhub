import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from src.api.routes.skills import download_skill
from src.storage.downloader import (
    SkillArchive,
    SkillArchiveConflictError,
    SkillArchiveNotFoundError,
)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/download",
            "headers": [(b"user-agent", b"pytest")],
            "client": ("127.0.0.1", 12345),
        }
    )


@pytest.mark.asyncio
async def test_download_skill_returns_file_and_commits_statistics(tmp_path):
    archive_path = tmp_path / "clean-code.zip"
    archive_path.write_bytes(b"zip")
    repository = SimpleNamespace(id=uuid.uuid4())
    skill = SimpleNamespace(id=uuid.uuid4(), skill_repo=repository)

    skill_repository = MagicMock()
    skill_repository.get_with_repository_by_skill_id = AsyncMock(return_value=skill)
    skill_repository.increment_download = AsyncMock(return_value=True)
    history_repository = MagicMock()
    history_repository.create = AsyncMock()
    digest = "a" * 64
    manager = MagicMock()
    manager.create_skill_archive = AsyncMock(
        return_value=SkillArchive(
            path=archive_path,
            filename="clean-code-1.1.0.zip",
            sha256=digest,
        )
    )
    db = AsyncMock()

    with (
        patch("src.api.routes.skills.SkillRepository", return_value=skill_repository),
        patch(
            "src.api.routes.skills.DownloadHistoryRepository",
            return_value=history_repository,
        ),
        patch("src.api.routes.skills.DownloadManager", return_value=manager),
    ):
        response = await download_skill(
            skill_id="gitcode/openeuler/repo/skills/clean-code",
            request=_request(),
            db=db,
        )

    assert isinstance(response, FileResponse)
    assert Path(response.path) == archive_path
    assert response.media_type == "application/zip"
    # 制品摘要通过响应头回传，供客户端校验下载内容
    assert response.headers["x-artifact-sha256"] == digest
    manager.create_skill_archive.assert_awaited_once_with(
        skill=skill,
        repository=repository,
    )
    history_repository.create.assert_awaited_once()
    skill_repository.increment_download.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("archive_error", "expected_status"),
    [
        (SkillArchiveNotFoundError("missing"), 404),
        (SkillArchiveConflictError("conflict"), 409),
    ],
)
async def test_download_skill_maps_archive_errors(archive_error, expected_status):
    skill = SimpleNamespace(id=uuid.uuid4(), skill_repo=SimpleNamespace())
    skill_repository = MagicMock()
    skill_repository.get_with_repository_by_skill_id = AsyncMock(return_value=skill)
    manager = MagicMock()
    manager.create_skill_archive = AsyncMock(side_effect=archive_error)
    db = AsyncMock()

    with (
        patch("src.api.routes.skills.SkillRepository", return_value=skill_repository),
        patch("src.api.routes.skills.DownloadManager", return_value=manager),
        pytest.raises(HTTPException) as error,
    ):
        await download_skill(
            skill_id="gitcode/openeuler/repo/skills/clean-code",
            request=_request(),
            db=db,
        )

    assert error.value.status_code == expected_status
    db.commit.assert_not_awaited()
