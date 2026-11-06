"""安全审计触发：为一条已入库的 skill 记录发起 Jenkins 检测。

扫描路径（``src.skills.skill_scanner``）与安全补齐路径
（``src/ai/security_backfill.py``）共用本实现，保证物化 blob、async 触发、
失败降级的口径完全一致。

``git_ops`` 为 ``src.utils.git_operations`` 的 GitOperations（爬虫与补齐
共用的 git 基础设施）；``repo`` / 记录字段由调用方提供（扫描路径传 ORM
实例，补齐路径传字段视图）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from src.security.detector import SecurityDetector, SecurityReport
from src.utils.git_operations import GitOperations

logger = logging.getLogger(__name__)


async def trigger_existing_skill_audit(
    *,
    git_ops: GitOperations,
    security_detector: SecurityDetector | None,
    security_async_mode: bool,
    repo: Any,
    relative_path: str,
    commit_id: str | None,
    skill_id: str,
) -> SecurityReport | None:
    """为一条已入库的 skill 记录触发安全审计。

    ``security_detector`` 为 None 或未配置 skillspector 时返回 None，
    由调用方决定跳过。
    """
    if security_detector is None or not security_detector.has_skillspector:
        return None
    skill_path = Path(relative_path).parent.as_posix()
    if skill_path == '.':
        skill_path = ''

    # skillspector 容器以只读方式挂载缓存仓库且无网络访问，
    # 此处提前物化技能路径的所有 blob，避免容器内触发懒拉取失败。
    if repo.local_path and commit_id:
        materialized = await asyncio.to_thread(
            git_ops.materialize_skill_objects,
            Path(repo.local_path), commit_id, skill_path,
        )
        if not materialized:
            logger.warning(
                'Security audit skipped for skill %s: objects not materialized',
                skill_id,
            )
            return SecurityReport(
                resource_type='skill',
                resource_id=skill_id,
                risk_level='unknown',
                risk_signals=[],
                details={'error': 'materialize_failed', 'source': 'skillspector'},
            )

    try:
        if security_async_mode:
            build_number = await security_detector.trigger_skillspector(
                repo.url, version=commit_id, skill_path=skill_path,
            )
            report = SecurityReport(
                resource_type='skill',
                resource_id=skill_id,
                risk_level='unknown',
                risk_signals=[],
                details={
                    'skillspector_async': True,
                    'skillspector_build_number': build_number,
                    'source': 'skillspector',
                },
            )
        else:
            report = await security_detector.detect_skillspector(
                repo.url, version=commit_id, skill_path=skill_path,
            )
    except Exception:
        logger.warning(
            'Security audit failed for skill %s', skill_id, exc_info=True,
        )
        failed_details: dict[str, Any] = {
            'error': 'audit_failed',
            'source': 'skillspector',
        }
        if security_async_mode:
            # 保持与触发失败落库路径一致：标记 async 尝试已发起，
            # 以便调用方写入 risk_level=unknown 审计记录
            failed_details['skillspector_async'] = True
            failed_details['skillspector_build_number'] = None
        report = SecurityReport(
            resource_type='skill',
            resource_id=skill_id,
            risk_level='unknown',
            risk_signals=[],
            details=failed_details,
        )

    return report
