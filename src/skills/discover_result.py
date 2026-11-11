"""discover 结果输出与 skill 统计（CLI 与自动调度共用，口径完全一致）。

- 结果文档构建 / JSON 落盘：``build_discover_result_document`` /
  ``save_discover_result_file``（remediation 结果文件同样复用后者）
- skill 级统计：``collect_skill_stats``（分类分布 / 安全检测 / 索引创建）
- 结果分类：``classify_discover_result``
- ``<key>_note`` 说明：counters / skill_stats / remediation_run 各字段含义，
  随结果 JSON 内嵌输出，便于离线解读

从 ``skillcrawler/main.py`` 拆出：本模块只依赖 stdlib + sqlalchemy +
src.core.config，src/jobs 内部（discover_scheduler / remediation）同层引用，
不再反向依赖 skillcrawler CLI 入口。
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ============ discover 结果 JSON（CLI 与自动调度共用，口径完全一致） ============

# counters 各项含义，随结果文件内嵌为 <key>_note，便于离线解读（与设计文档 §11 口径一致）
COUNTER_NOTES: dict[str, str] = {
    "created": "新建仓库记录并完成扫描",
    "rediscovered": "已有仓库 commit 变化并完成重扫",
    "unchanged": "HEAD commit 与数据库相同，跳过扫描",
    "removed": "已有仓库不再包含可扫描 Skill，删除 DB 仓库记录",
    "no_skill": "新仓库没有可扫描 Skill，不创建 DB 仓库记录",
    "failed": "当前仓库处理失败（clone/fetch 或扫描异常），详见 repos[].error",
}

# skill_stats / remediation_run 各项含义，同样以 <key>_note 内嵌输出（与设计文档 §11 口径一致）
SKILL_STATS_NOTES: dict[str, str] = {
    "total_skills": "skills 表 Skill 总数（latest Skill，不含 Tag 版本）",
    "category_distribution": "分类分布 {category: count}，按数量降序；NULL / 空 / other / others 统一归入 others",
    "security_audit": "安全检测结果，各子项含义见其 <key>_note",
    "index_creation": "索引创建状态（互斥三分法，indexed + missing + not_required = total），各子项含义见其 <key>_note",
}
SECURITY_AUDIT_NOTES: dict[str, str] = {
    "audited": "安全检测完成（risk_score 非空）的 Skill 数",
    "pending": "异步检测进行中（有 skillspector_collected 为空的检测记录）的 Skill 数",
    "failed": "安全检测失败：risk_score 为空且无异步检测进行中",
    "failed_skills": "失败明细（skill_id / name / source_url），最多 50 条",
    "failed_skills_truncated": "失败明细被截断标记（超出 50 条只计数不列明细）",
}
INDEX_CREATION_NOTES: dict[str, str] = {
    "total": "skills 表 Skill 总数",
    "indexed": "已建 embedding 的 Skill 数（不论是否已审计）",
    "missing": "已审计但 embedding 缺失的 Skill 数（索引创建失败，等下轮 discover 或 API 启动补跑自愈）",
    "not_required": "未审计且未建索引（risk_score 与 embedding 均为空）的 Skill 数，暂不建索引属预期状态",
}
SECURITY_BACKFILL_NOTES: dict[str, str] = {
    "total": "本轮扫描到的待补审计行数（risk_score 为空且无在途异步检测）",
    "triggered": "本轮成功重新触发 Jenkins 检测的行数",
    "failed": "触发失败（未获得 build number）的行数",
    "skipped": "本轮跳过补审计的原因",
}
CATEGORY_BACKFILL_NOTES: dict[str, str] = {
    "total": "本轮扫描到的空分类行数",
    "filled": "本轮成功写入分类的行数",
    "unclassified": "分类器无结果（frontmatter 也无合法值）的行数",
    "failed": "分类调用抛错的行数",
    "skipped": "本轮跳过补分类的原因",
}
EMBEDDING_RUN_NOTES: dict[str, str] = {
    "total": "本轮扫描到的待编码行数",
    "indexed": "本轮成功写入 embedding 的行数",
    "failed_batches": "embedding 请求或写库失败的批次数",
    "failed_rows": "embedding 请求或响应校验失败的行数",
    "skipped_empty": "文本为空被跳过的行数",
    "stale_skipped": "编码期间内容发生变化或记录消失的行数",
    "skipped": "本轮跳过补建的原因",
}
# remediation_run 三个补齐任务各自的 <key>_note 口径
REMEDIATION_TASK_NOTES: dict[str, dict[str, str]] = {
    "security": SECURITY_BACKFILL_NOTES,
    "category": CATEGORY_BACKFILL_NOTES,
    "embedding": EMBEDDING_RUN_NOTES,
}


def _with_notes(data: dict[str, Any], notes: dict[str, str]) -> dict[str, Any]:
    """每个 <key> 后补 <key>_note 说明，便于离线解读（与 counters 同风格）。"""
    merged: dict[str, Any] = {}
    for key, value in data.items():
        merged[key] = value
        merged[f"{key}_note"] = notes.get(key, "")
    return merged


def remediation_run_with_notes(
    remediation_run: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """remediation_run 输出前为各任务的每个字段补 <key>_note 说明。"""
    if remediation_run is None:
        return None
    merged: dict[str, Any] = {}
    for task_name, task_stats in remediation_run.items():
        if isinstance(task_stats, dict):
            merged[task_name] = _with_notes(
                task_stats, REMEDIATION_TASK_NOTES.get(task_name, {})
            )
        else:
            merged[task_name] = task_stats
    return merged


def skill_stats_with_notes(skill_stats: dict[str, Any]) -> dict[str, Any]:
    """skill_stats 输出前补 <key>_note，security_audit / index_creation 子对象同样补。"""
    merged = _with_notes(skill_stats, SKILL_STATS_NOTES)
    security = skill_stats.get("security_audit")
    if isinstance(security, dict):
        merged["security_audit"] = _with_notes(security, SECURITY_AUDIT_NOTES)
    index = skill_stats.get("index_creation")
    if isinstance(index, dict):
        merged["index_creation"] = _with_notes(index, INDEX_CREATION_NOTES)
    return merged


# security_audits 中「异步检测仍在进行」的判定条件（JSONB 字段，无外键，
# resource_type='skill' + resource_id 关联 skills.id / skill_versions.id）
_PENDING_ASYNC_AUDIT_EXISTS = """
EXISTS (
    SELECT 1 FROM security_audits a
    WHERE a.resource_type = 'skill' AND a.resource_id = skills.id
      AND coalesce((a.details ->> 'skillspector_async')::boolean, false)
      AND a.details ->> 'skillspector_collected' IS NULL
)
"""
_NO_PENDING_ASYNC_AUDIT = f"NOT ({_PENDING_ASYNC_AUDIT_EXISTS})"

# 安全检测失败 skill_id 列表的输出上限，避免结果文件过大
FAILED_SKILL_IDS_LIMIT = 50


async def collect_skill_stats(session: Any) -> dict[str, Any]:
    """skill 级统计：分类分布、安全检测结果、索引创建状态。

    口径：以 skills 表（latest Skill）为准；skill_versions（Tag 版本）不计入。
    安全检测「失败」= risk_score 仍为空且无异步检测在进行（触发失败或终态
    无报告）；「进行中」= 存在 skillspector_collected 为空的异步检测记录。
    查询失败由调用方兜底，不影响主流程。
    """
    from sqlalchemy import case, func, select, text

    from src.models.orm import Skill

    # 1. 分类分布（NULL/空/other/others 统一归入 others）
    category_expr = func.lower(func.nullif(func.trim(Skill.category), ""))
    normalized_category = case(
        (category_expr.is_(None), "others"),
        (category_expr.in_(["other", "others"]), "others"),
        else_=category_expr,
    )
    category_rows = await session.execute(
        select(normalized_category.label("category_key"), func.count())
        .group_by("category_key")
        .order_by(func.count().desc())
    )
    category_distribution = {
        row.category_key: row[1] for row in category_rows.fetchall()
    }

    # 2. 安全检测：按 risk_score 是否就绪 + 是否有异步检测进行中拆分
    security_rows = await session.execute(
        select(
            func.count(),
            func.count().filter(Skill.risk_score.is_not(None)),
            func.count().filter(
                Skill.risk_score.is_(None),
                text(_PENDING_ASYNC_AUDIT_EXISTS),
            ),
        )
    )
    total_skills, audited, security_pending = security_rows.one()
    security_failed = total_skills - audited - security_pending

    # 失败明细（上限 FAILED_SKILL_IDS_LIMIT 条，超出部分只计数）
    failed_rows = await session.execute(
        select(Skill.skill_id, Skill.name, Skill.source_url)
        .where(
            Skill.risk_score.is_(None),
            text(_NO_PENDING_ASYNC_AUDIT),
        )
        .order_by(Skill.skill_id)
        .limit(FAILED_SKILL_IDS_LIMIT)
    )
    security_failed_skills = [
        {"skill_id": row.skill_id, "name": row.name, "source_url": row.source_url}
        for row in failed_rows.fetchall()
    ]

    # 3. 索引创建（embedding）：indexed / missing / not_required 为互斥三分法，
    #    各项之和恒等于 total。只有已审计（risk_score 非空）的行才会建索引，
    #    未审计且未建索引的 embedding 为空是预期状态，单列出来避免误判为失败。
    index_rows = await session.execute(
        select(
            func.count(),
            func.count().filter(Skill.embedding.is_not(None)),
            func.count().filter(
                Skill.embedding.is_(None), Skill.risk_score.is_not(None)
            ),
            func.count().filter(
                Skill.embedding.is_(None), Skill.risk_score.is_(None)
            ),
        )
    )
    index_total, indexed, index_missing, index_not_required = index_rows.one()

    return {
        "total_skills": total_skills,
        "category_distribution": category_distribution,
        "security_audit": {
            "audited": audited,
            "pending": security_pending,
            "failed": security_failed,
            "failed_skills": security_failed_skills,
            "failed_skills_truncated": security_failed > len(security_failed_skills),
        },
        "index_creation": {
            "total": index_total,
            "indexed": indexed,
            "missing": index_missing,
            "not_required": index_not_required,
        },
    }


def log_skill_stats(skill_stats: dict[str, Any]) -> None:
    """skill 统计输出到日志（多行结构化，便于排查时直接查看）。

    汇总一行 + 分类分布一行 + 安全检测失败明细逐条（上限
    FAILED_SKILL_IDS_LIMIT，超出只记总数），与结果文件同口径。
    """
    security = skill_stats.get("security_audit", {})
    index = skill_stats.get("index_creation", {})
    logger.info(
        "[Discover] Skill stats after discover: total_skills=%s, security: audited=%s "
        "pending=%s failed=%s, index: total=%s indexed=%s missing=%s "
        "not_required=%s",
        skill_stats.get("total_skills"),
        security.get("audited"),
        security.get("pending"),
        security.get("failed"),
        index.get("total"),
        index.get("indexed"),
        index.get("missing"),
        index.get("not_required"),
    )
    category_distribution = skill_stats.get("category_distribution", {})
    if category_distribution:
        category_text = ", ".join(
            f"{name}={count}"
            for name, count in category_distribution.items()
        )
        logger.info("[Discover] Skill category distribution: %s", category_text)
    failed_skills = security.get("failed_skills") or []
    for item in failed_skills:
        logger.warning(
            "[Discover] Security audit failed skill: skill_id=%s name=%s source_url=%s",
            item.get("skill_id"),
            item.get("name"),
            item.get("source_url"),
        )
    if security.get("failed_skills_truncated"):
        logger.warning(
            "[Discover] Security audit failed skills truncated: %s more not listed "
            "(limit=%d)",
            security.get("failed", 0) - len(failed_skills),
            FAILED_SKILL_IDS_LIMIT,
        )


def classify_discover_result(result: Any) -> str:
    """按 manager 标记的私有标志对单仓结果分类（CLI 与自动调度同口径）。"""
    if result is None:
        return "no_skill"
    if getattr(result, "_removed_existing", False):
        return "removed"
    if getattr(result, "_unchanged", False):
        return "unchanged"
    if getattr(result, "_created_new", False):
        return "created"
    return "rediscovered"


def resolve_discover_result_dir(result_dir: str = "") -> Path:
    """结果保存目录：优先显式 result_dir 配置，否则 storage.local_path/logs。"""
    if result_dir.strip():
        return Path(result_dir.strip()).expanduser()
    from src.core.config import get_settings

    configured = get_settings().discover_scheduler.result_dir.strip()
    if configured:
        return Path(configured).expanduser()
    return Path(get_settings().storage.local_path) / "logs"


def _cleanup_old_result_dirs(target_root: Path, retain_days: int = 30) -> None:
    """删除早于 retain_days 天的日期归档子目录（如 logs/20261111/）。

    结果文件按日期归档到 YYYYMMDD 子目录，长期运行会持续累积；这里按目录名
    解析日期，删除早于保留期的目录。整体失败仅记录 warning，不影响主流程。
    """
    cutoff = datetime.now() - timedelta(days=retain_days)
    try:
        entries = list(target_root.iterdir())
    except Exception:
        logger.warning(
            "Failed to list result dir %s for cleanup", target_root, exc_info=True
        )
        return
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            parsed = datetime.strptime(entry.name, "%Y%m%d")
        except ValueError:
            continue
        if parsed < cutoff:
            try:
                shutil.rmtree(entry)
                logger.info("[Discover] removed stale result dir %s", entry)
            except Exception:
                logger.warning(
                    "Failed to remove stale result dir %s", entry, exc_info=True
                )


def save_discover_result_file(
    document: dict[str, Any], result_dir: str = "", file_prefix: str = "discover",
    log: bool = True,
) -> Path | None:
    """把一轮结果写入 JSON 文件；失败仅记录 warning，不影响主流程。

    file_prefix 决定文件名前缀（``<prefix>-<时间戳>.json``）；补齐循环
    （src/jobs/remediation.py）以 ``remediation`` 前缀复用本函数并以
    ``log=False`` 关掉这里的成功日志，改用自身的 ``[Remediation]`` 前缀打印，
    避免同一结果文件重复打印成功日志。
    """
    try:
        now = datetime.now()
        # 结果文件按日期归档到子目录（如 logs/20261111/），便于按天检索与清理
        target_root = resolve_discover_result_dir(result_dir)
        date_dir = now.strftime("%Y%m%d")
        target_dir = target_root / date_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        timestamp = now.strftime("%Y%m%d-%H%M%S")
        path = target_dir / f"{file_prefix}-{timestamp}.json"
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if log:
            logger.info("[Discover] %s result saved to %s", file_prefix, path)
        # 清理超过一个月的日期归档目录，避免结果文件无限累积
        _cleanup_old_result_dirs(target_root)
        return path
    except Exception:
        # 前缀随 file_prefix 变化：discover 调用打 "discover"，补齐调用打
        # "remediation"（见 src/jobs/remediation.py）
        logger.warning("Failed to save %s result file", file_prefix, exc_info=True)
        return None


def build_discover_result_document(
    *,
    started_at: datetime,
    run_started: float,
    total_repos: int,
    config_keys: list[str],
    counters: dict[str, int],
    repo_details: list[dict[str, Any]],
    remediation_run: dict[str, Any] | None,
    skill_stats: dict[str, Any] | None,
) -> dict[str, Any]:
    """构建一轮 discover 的结果文档（CLI 与自动调度共用，口径完全一致）。

    skill_stats 由调用方采集传入（查询失败传 None）；remediation_run 为
    security / category / embedding 三个补齐任务的统计（详见
    src/jobs/remediation.py）。counters / skill_stats / remediation_run
    的每个字段均内嵌 <key>_note 说明。耗时与最慢仓库统计由 repo_details 推导。
    """
    if skill_stats is not None:
        log_skill_stats(skill_stats)

    run_elapsed = time.monotonic() - run_started
    avg_elapsed = run_elapsed / total_repos if total_repos else 0.0
    durations = [
        (detail.get("url", ""), detail.get("duration_seconds", 0.0))
        for detail in repo_details
    ]
    slowest = sorted(durations, key=lambda item: item[1], reverse=True)[:3]

    return {
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "total_repos": total_repos,
        "config_keys": config_keys,
        "counters": _with_notes(counters, COUNTER_NOTES),
        "skill_stats": skill_stats_with_notes(skill_stats)
        if skill_stats is not None
        else None,
        "remediation_run": remediation_run_with_notes(remediation_run)
        if remediation_run is not None
        else None,
        "total_seconds": round(run_elapsed, 3),
        "avg_seconds_per_repo": round(avg_elapsed, 3),
        "slowest_repos": [
            {"url": label, "seconds": round(duration, 3)}
            for label, duration in slowest
        ],
        "repos": repo_details,
    }
