"""license 判定运行时：统一构建 checker，供定时任务与 discover 共用。"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from src.core.config import LicenseAuditConfig, Settings, get_settings
from src.skills.license.access_list import load_access_list
from src.skills.license.gate_engine import (
    STATUS_ALLOWED,
    STATUS_BLACK,
    STATUS_CONFLICT,
    STATUS_ERROR,
    STATUS_MISSING,
    STATUS_RESTRICTED,
    STATUS_UNKNOWN,
    LicenseVerdict,
)
from src.skills.license.scanner import LicenseChecker

logger = logging.getLogger(__name__)

_checker: LicenseChecker | None = None
_lock = threading.Lock()


def _default_cache_path(settings: Settings) -> str:
    return str(Path(settings.storage.local_path) / "license" / "Licenses_Access_List.yaml")


def build_checker(settings: Settings | None = None, *, required: bool = True) -> LicenseChecker | None:
    """按当前配置构建 checker；清单不可用时按 required 决定抛错或返回 None。"""
    settings = settings or get_settings()
    config: LicenseAuditConfig = settings.license_audit
    cache_path = config.gate.access_list_cache or _default_cache_path(settings)
    access = load_access_list(
        url=config.gate.access_list_url,
        cache_path=cache_path,
        ttl_hours=config.gate.access_list_ttl_hours,
        extra_allow=list(config.gate.extra_allow),
        timeout=config.gate.access_list_timeout,
        required=required,
    )
    if access is None:
        return None
    remote_cache_root = config.repo.remote_cache_root or str(
        Path(settings.storage.local_path) / "license-repos"
    )
    return LicenseChecker(
        access,
        prefer_file=config.gate.prefer_file,
        remote_cache_root=remote_cache_root,
        remote_cache_ttl_days=config.repo.remote_cache_ttl_days,
        remote_fetch=config.repo.remote_fetch,
        git_timeout=config.repo.git_timeout,
    )


def get_license_checker(required: bool = False) -> LicenseChecker | None:
    """进程内复用 checker（discover 逐仓库扫描时避免重复加载清单）。"""
    global _checker
    with _lock:
        if _checker is not None:
            return _checker
        checker = build_checker(required=required)
        if checker is not None:
            _checker = checker
        return checker


def reset_license_checker() -> None:
    """测试用：清空进程内缓存。"""
    global _checker
    with _lock:
        _checker = None


def action_for_status(config: LicenseAuditConfig, status: str) -> str:
    """状态 -> success / warning / failed，与门禁 ac.yaml 的动作配置一致。"""
    actions = config.actions
    return {
        STATUS_ALLOWED: actions.allowed,
        STATUS_CONFLICT: actions.conflict,
        STATUS_RESTRICTED: actions.restricted,
        STATUS_MISSING: actions.missing,
        STATUS_UNKNOWN: actions.unknown,
        STATUS_BLACK: actions.black,
        STATUS_ERROR: actions.unknown,
    }.get(status, "failed")


def is_compliant(config: LicenseAuditConfig, verdict: LicenseVerdict) -> bool:
    """success / warning 视为合规；error 不算合规，但调用方不应据此删除。"""
    return action_for_status(config, verdict.status) in ("success", "warning")
