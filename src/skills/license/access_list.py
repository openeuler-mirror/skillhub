"""license 准入清单加载：远端拉取、本地缓存与过期降级。

与门禁共用同一份 compliance SIG 清单；缓存只用于减少网络请求，
拉取失败时优先使用上一次成功缓存，避免因网络波动误删。
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from src.skills.license.gate_engine import LicenseAccessList, LicenseListError

logger = logging.getLogger(__name__)


class LicenseListUnavailableError(RuntimeError):
    """清单既无法拉取、也没有可用缓存。"""


def _download(url: str, timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise LicenseListError(f"HTTP {status}")
            return response.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LicenseListError(str(exc)) from exc


def _cache_fresh(path: Path, ttl_hours: int) -> bool:
    if ttl_hours <= 0 or not path.is_file():
        return False
    age = time.time() - path.stat().st_mtime
    return age < ttl_hours * 3600


def _write_cache(path: Path, payload: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    except OSError:
        logger.warning("write license list cache failed: %s", path, exc_info=True)


def load_access_list(
    url: str,
    cache_path: str,
    ttl_hours: int,
    extra_allow: list[str],
    timeout: float = 30.0,
    required: bool = True,
) -> LicenseAccessList | None:
    """加载准入清单。

    顺序：新鲜缓存 -> 远端拉取（成功后刷新缓存） -> 过期缓存。
    全部失败时 required=True 抛 LicenseListUnavailableError，否则返回 None。
    """
    cache = Path(cache_path).expanduser() if cache_path else None
    if cache is not None and _cache_fresh(cache, ttl_hours):
        try:
            access = LicenseAccessList(source=str(cache), extra_allow=extra_allow).load()
            logger.info(
                "license list loaded from cache: entries=%d hash=%s",
                len(access._entries), access.hash,
            )
            return access
        except LicenseListError:
            logger.warning("license list cache invalid, falling back to remote", exc_info=True)

    payload = None
    try:
        payload = _download(url, timeout)
    except LicenseListError as exc:
        logger.warning("license list download failed: %s", exc)

    if payload:
        if cache is not None:
            _write_cache(cache, payload)
        try:
            access = LicenseAccessList(
                source=str(cache) if cache is not None else url,
                extra_allow=extra_allow,
            ).load()
            logger.info(
                "license list loaded: entries=%d hash=%s", len(access._entries), access.hash,
            )
            return access
        except LicenseListError as exc:
            logger.warning("license list parse failed: %s", exc)

    if cache is not None and cache.is_file():
        try:
            access = LicenseAccessList(source=str(cache), extra_allow=extra_allow).load()
            logger.warning(
                "license list using stale cache: entries=%d hash=%s",
                len(access._entries), access.hash,
            )
            return access
        except LicenseListError:
            logger.error("license list stale cache invalid", exc_info=True)

    message = "license 准入清单不可用（无缓存且拉取失败）"
    if required:
        raise LicenseListUnavailableError(message)
    logger.warning(message)
    return None
