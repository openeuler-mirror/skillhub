import asyncio

import pytest

from src.storage.downloader import MAX_STORAGE_READ_BYTES, LocalStorage


@pytest.mark.parametrize(
    ("skill_id", "filename"),
    [
        ("../outside", "secret.txt"),
        ("skill", "../../secret.txt"),
        ("/tmp/outside", "secret.txt"),
        ("skill", "/tmp/secret.txt"),
    ],
)
def test_local_storage_rejects_paths_outside_base(tmp_path, skill_id, filename):
    storage = LocalStorage(tmp_path / "storage")

    with pytest.raises(ValueError, match="escapes base directory"):
        storage._resolve_path(skill_id, filename)


def test_local_storage_rejects_symlink_escape(tmp_path):
    storage_root = tmp_path / "storage"
    outside = tmp_path / "outside"
    storage_root.mkdir()
    outside.mkdir()
    (storage_root / "linked").symlink_to(outside, target_is_directory=True)
    storage = LocalStorage(storage_root)

    with pytest.raises(ValueError, match="escapes base directory"):
        storage._resolve_path("linked", "secret.txt")


def test_local_storage_rejects_oversized_read(tmp_path):
    storage = LocalStorage(tmp_path / "storage")
    file_path = storage._resolve_path("skill", "large.bin")
    file_path.parent.mkdir(parents=True)
    with file_path.open("wb") as file:
        file.truncate(MAX_STORAGE_READ_BYTES + 1)

    with pytest.raises(ValueError, match="File is too large"):
        asyncio.run(storage.read("skill", "large.bin"))


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("save", ("../outside", "content", "secret.txt")),
        ("read", ("../outside", "secret.txt")),
        ("delete", ("../outside",)),
        ("list_files", ("../outside",)),
    ],
)
def test_local_storage_operations_reject_unsafe_paths(tmp_path, operation, arguments):
    storage = LocalStorage(tmp_path / "storage")

    with pytest.raises(ValueError, match="escapes base directory"):
        asyncio.run(getattr(storage, operation)(*arguments))
