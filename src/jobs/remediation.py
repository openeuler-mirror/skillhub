"""统一补齐（remediation）：衍生字段缺口的补跑编排。

skills / skill_versions 上的三个衍生字段会在降级路径下缺失（检测触发失败、
LLM 超时、写库中断），且内容不变时 reconcile 不重扫、缺口不会自行消失。
本模块把三类补跑统一为「查缺口 → 补写 → 统计」的任务，由 ``run_remediations()``
依次执行，三个补跑实现各归其域：

- security：``risk_score`` 缺口 → ``src/ai/security_backfill.py``（重新触发
  检测，评分由 SkillspectorCollector 异步回填）；
- category：``category`` 缺口 → ``src/ai/category_backfill.py``（分类器补跑）；
- embedding：``embedding`` 缺口 → ``src/ai/embedding_indexer.py`` 的
  ``embed_missing``（向量补建）。

三类缺口互不影响（embedding 只看 risk_score 已就绪的存量行）；各任务自持
pg_advisory_lock（embedding 的锁另与全量重建脚本互斥），单任务锁被占用或
异常只记入该任务统计，不影响其余任务。

触发点：每轮 discover 末尾（skillcrawler CLI 与 DiscoverScheduler）。本轮新
入库的缺口行立即补齐，不必等启动。
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.config import get_settings

logger = logging.getLogger(__name__)
LOG_PREFIX = "[Remediation]"


# ── 补齐任务：run_remediations 依次执行 security / category / embedding ──
# 三个包装同款：局部导入实现 → 调用 → 锁冲突记 skipped；单任务异常由
# run_remediations 兜底记入该任务统计。


async def _run_security() -> dict[str, Any]:
    from src.ai.security_backfill import (
        LockBusyError as SecurityLockBusyError,
        backfill_missing_security_audits,
    )

    try:
        stats = await backfill_missing_security_audits()
        return stats.as_dict()
    except SecurityLockBusyError:
        logger.info("%s security skipped: lock held by another task", LOG_PREFIX)
        return {"skipped": "security backfill lock held by another task"}


async def _run_category() -> dict[str, Any]:
    from src.ai.category_backfill import (
        LockBusyError as CategoryLockBusyError,
        backfill_missing_categories,
    )

    try:
        stats = await backfill_missing_categories()
        return stats.as_dict()
    except CategoryLockBusyError:
        logger.info("%s category skipped: lock held by another task", LOG_PREFIX)
        return {"skipped": "category backfill lock held by another task"}


async def _run_embedding() -> dict[str, Any]:
    if not get_settings().ai.enable_semantic_search:
        logger.info("%s embedding skipped: semantic search disabled", LOG_PREFIX)
        return {"skipped": "semantic search disabled"}

    from src.ai.embedding_indexer import (
        LockBusyError as EmbeddingLockBusyError,
        embed_missing,
    )

    try:
        stats = await embed_missing()
        return stats.as_dict()
    except EmbeddingLockBusyError:
        logger.info("%s embedding skipped: reindex lock held by another task", LOG_PREFIX)
        return {"skipped": "reindex lock held by another task"}


# 补齐任务执行顺序：security → category → embedding（仅为固定执行顺序，
# 三类字段缺口互不影响）。按函数名解析而非直接引用，便于测试替换单个任务实现。
_REMEDIATION_TASKS = (
    ("security", "_run_security"),
    ("category", "_run_category"),
    ("embedding", "_run_embedding"),
)


async def run_remediations() -> dict[str, Any]:
    """依次补跑 security / category / embedding 三类字段缺口。

    三类缺口互不影响，顺序仅为固定执行顺序。返回 ``{任务名: 统计 dict}``，
    随 discover 结果 JSON 落盘；单任务异常只记录在该任务的统计里，
    不影响后续任务。
    """
    results: dict[str, Any] = {}
    for name, func_name in _REMEDIATION_TASKS:
        logger.info("%s task started: %s", LOG_PREFIX, name)
        try:
            results[name] = await globals()[func_name]()
        except Exception:
            logger.exception("%s task failed: %s", LOG_PREFIX, name)
            results[name] = {"error": "remediation task failed"}
        logger.info("%s task finished: %s -> %s", LOG_PREFIX, name, results[name])
    return results
