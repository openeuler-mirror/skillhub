"""license 判定运行时：构建 checker，并把判定状态映射为数据库三态。

本模块是判定引擎与任务/配置之间的唯一胶水层：

1. ``build_checker`` 读取 ``Settings.license_check``，加载准入清单并创建
   ``LicenseChecker``。清单不可用时按 ``required`` 决定抛错还是返回 None；
   定时任务使用 ``required=False``，此时本轮跳过、不写 0。
2. ``verdict_to_flag`` 把引擎状态映射为 ``license_compliant`` 三态：

   ============================  ==============
   引擎状态                      写入值
   ============================  ==============
   allowed                       ``1``
   black / restricted            ``0``
   missing / unknown             ``0``
   error / 其他未知状态           ``None``（不写库）
   ============================  ==============

   conflict 不会作为终态出现：``gate_engine.scan`` 在声明与 LICENSE 文件
   不一致时按 SKILL.md 声明返回，冲突原因只记在 verdict.reasons 中。
3. ``verdict_to_license`` 从同一次判定中提取「具体 license 类型」
   （``verdict.expression``），供 ``skills.license`` / ``skill_versions.license``
   字段落库。它与 ``license_compliant`` 共用一次判定结果，不再额外扫描仓库。
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.core.config import LicenseCheckConfig, Settings, get_settings
from src.skills.license.access_list import load_access_list
from src.skills.license.gate_engine import (
    STATUS_ALLOWED,
    STATUS_BLACK,
    STATUS_MISSING,
    STATUS_RESTRICTED,
    STATUS_UNKNOWN,
    LicenseVerdict,
)
from src.skills.license.scanner import LicenseChecker

logger = logging.getLogger(__name__)

# license_compliant 三态：1 合规 / 0 不合规 / None 未完成检查（如清单不可用）
FLAG_COMPLIANT = 1
FLAG_NON_COMPLIANT = 0


def _default_cache_path(settings: Settings) -> str:
    """准入清单默认缓存路径（落在持久卷上，容器重启后可复用）。"""
    return str(Path(settings.storage.local_path) / "license" / "Licenses_Access_List.yaml")


def build_checker(settings: Settings | None = None, *, required: bool = True) -> LicenseChecker | None:
    """按当前配置构建 checker。

    :param settings: 显式传入配置（测试/任务），默认使用进程级 ``get_settings()``
    :param required:
        - ``True``：清单不可用时抛 ``LicenseListUnavailableError``；
        - ``False``：清单不可用时返回 ``None``，由调用方跳过本轮。
          定时任务必须用 ``False``，否则网络波动会导致整轮任务失败。
    :return: 可复用的 ``LicenseChecker`` 或 ``None``

    注意：checker 内部持有目录级 license 文件缓存，应在**一轮任务**内复用，
    不要跨轮长期持有（否则仓库更新后可能读到过期缓存）。
    """
    settings = settings or get_settings()
    config: LicenseCheckConfig = settings.license_check
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
    # 远程缓存用于本地 clone 缺失的仓库；路径默认落在 storage.local_path 下
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


def verdict_to_flag(status: str) -> int | None:
    """判定状态 -> ``license_compliant`` 三态。

    - allowed -> 1
    - black / restricted / missing / unknown -> 0
    - error 及其他未知状态 -> None（未完成检查，不写库，保留原值）

    conflict 已在判定层按 SKILL.md 声明解析，不再作为终态出现。
    """
    if status == STATUS_ALLOWED:
        return FLAG_COMPLIANT
    if status in (STATUS_BLACK, STATUS_RESTRICTED, STATUS_MISSING, STATUS_UNKNOWN):
        return FLAG_NON_COMPLIANT
    return None


def verdict_to_license(verdict: LicenseVerdict) -> str | None:
    """判定结论 -> 具体 license 类型（归一化后的 SPDX 表达式）。

    取值口径（与 ``verdict.expression`` 一致）：

    - 声明可解析：声明本身，如 ``MIT``、``MIT AND Apache-2.0``；
    - 声明缺失、由 LICENSE 文件识别：识别结果，如 ``Apache-2.0``；
    - missing（无声明且无 license 文件）：空字符串 -> ``None``；
    - unknown 但声明非空：保留原始声明，便于回溯「为什么判成不合规」；
    - error（仓库不可读等）：判定层不产生 expression -> ``None``。

    调用方约定：``verdict_to_flag`` 返回 ``None``（本轮未完成检查）时，
    **不要**写 ``license`` 字段，应与 ``license_compliant`` 一起保持原值，
    避免把基础设施故障误写成“没有 license”。
    """
    expression = (verdict.expression or "").strip()
    return expression or None
