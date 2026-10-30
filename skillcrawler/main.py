#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.skills.config import sync_openEuler_skills_repo
from src.ai.category_classifier import CategoryClassificationError
from src.skills.popularity import (
    PopularityCollector,
    allocate_skill_downloads,
    estimate_repo_downloads,
)
from src.skills.skill_manager import SkillManager, SkillRepositoryRequest
from src.core.config import get_settings
from src.core.database import get_db_context
from src.skills.discover_requests import (
    PLATFORM_CHOICES,
    _build_requests_from_skill_yaml,
    build_configured_discover_requests,
)
from src.skills.discover_result import (
    build_discover_result_document,
    classify_discover_result,
    collect_skill_stats,
    save_discover_result_file,
)
from src.models.repository import (
    ContributorRepository,
    SkillRepoRepository,
    SkillRepository,
)

settings = get_settings()

_LOG_DIR = Path(settings.storage.local_path).expanduser().resolve() / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_LEVEL = getattr(logging, settings.logging.level.strip().upper(), logging.INFO)

logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_LOG_DIR / "skillcrawler.log"),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
DISCOVER_RESULT_COLUMNS = [
    ("#", "#", 5),
    ("result", "result", 14),
    ("status", "status", 16),
    ("skills", "skills", 8),
    ("name", "name", 36),
    ("url", "url", 64),
    ("error", "error", 80),
]
def _format_skill_repo(repository: Any) -> str:
    display_name = _display_skill_repo_name(repository)
    return (
        f"repo_id={repository.id} "
        f"repo_name={display_name} "
        f"source={repository.source} "
        f"platform={repository.platform or '-'} "
        f"branch={repository.branch} "
        f"repo_url={repository.url} "
        f"status={repository.skill_discover_status} "
        f"skill_num={repository.skill_num}"
    )


def _display_skill_repo_name(repository: Any) -> str:
    repo_name = str(repository.repo_name or "")
    branch = str(repository.branch or "").strip()
    if branch and repo_name.endswith(f"@{branch}"):
        return repo_name[: -(len(branch) + 1)]
    return repo_name or "-"


def _format_exception(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return _format_process_error(exc)
    if isinstance(exc, subprocess.TimeoutExpired):
        return _format_timeout_error(exc)

    message = str(exc).strip()
    cause = exc.__cause__
    if isinstance(cause, subprocess.CalledProcessError):
        detail = _format_process_error(cause)
        return f"{message}\nCaused by:\n{detail}" if message else detail
    if isinstance(cause, subprocess.TimeoutExpired):
        detail = _format_timeout_error(cause)
        return f"{message}\nCaused by:\n{detail}" if message else detail

    if message:
        return message
    if cause is not None:
        cause_message = str(cause).strip()
        if cause_message:
            return cause_message
    return exc.__class__.__name__


def _format_process_error(exc: subprocess.CalledProcessError) -> str:
    lines = [
        f"Command failed with exit code {exc.returncode}: {_format_command(exc.cmd)}"
    ]
    stdout = str(exc.output or "").strip()
    stderr = str(exc.stderr or "").strip()
    if stdout:
        lines.append(f"stdout:\n{stdout}")
    if stderr:
        lines.append(f"stderr:\n{stderr}")
    if not stdout and not stderr:
        fallback = str(exc).strip()
        if fallback:
            lines.append(fallback)
    return "\n".join(lines)


def _format_timeout_error(exc: subprocess.TimeoutExpired) -> str:
    lines = [
        f"Command timed out after {exc.timeout}s: {_format_command(exc.cmd)}"
    ]
    stdout = str(exc.output or "").strip()
    stderr = str(exc.stderr or "").strip()
    if stdout:
        lines.append(f"stdout:\n{stdout}")
    if stderr:
        lines.append(f"stderr:\n{stderr}")
    return "\n".join(lines)


def _format_command(command: Any) -> str:
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return str(command)


def _print_failure_details(failures: list[dict[str, str]]) -> None:
    if not failures:
        return
    print()
    logger.error("[Discover] Failure details:")
    for failure in failures:
        logger.error("[Discover] [%s] %s", failure['#'], failure['url'])
        logger.error("%s", failure["error"])
        print()


def _print_failure_detail(index: str, url: str, error: str) -> None:
    logger.error("[Discover] [%s] failed: %s", index, url)
    logger.error("%s", error)
    print()


def _build_cli_error(action: str, message: str, tip: str | None = None) -> ValueError:
    lines = [f"{action} Error: {message}"]
    if tip:
        lines.append(f"Tip: {tip}")
    return ValueError("\n".join(lines))


def _status_counts(repositories: list[Any]) -> OrderedDict[str, int]:
    counts: OrderedDict[str, int] = OrderedDict()
    for repository in repositories:
        status = str(repository.skill_discover_status or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _clip(value: Any, max_width: int) -> str:
    text = str(value or "-")
    if len(text) <= max_width:
        return text
    if max_width <= 3:
        return text[:max_width]
    return text[: max_width - 3] + "..."


def _print_table(
    rows: list[dict[str, str]],
    columns: list[tuple[str, str, int]],
    *,
    empty_message: str,
) -> None:
    if not rows:
        print(empty_message)
        return

    normalized_rows = [
        {
            key: _clip(row.get(key, "-"), max_width)
            for key, _, max_width in columns
        }
        for row in rows
    ]
    widths = {
        key: max(len(label), *(len(row[key]) for row in normalized_rows))
        for key, label, _ in columns
    }

    header = "  ".join(label.ljust(widths[key]) for key, label, _ in columns)
    separator = "  ".join("-" * widths[key] for key, _, _ in columns)
    print(header)
    print(separator)
    for row in normalized_rows:
        print("  ".join(row[key].ljust(widths[key]) for key, _, _ in columns))


def _print_repository_table(repositories: list[Any]) -> None:
    if not repositories:
        print("No skill repos found.")
        return

    rows: list[dict[str, str]] = []
    for repository in repositories:
        rows.append(
            {
                "id": str(repository.id),
                "status": str(repository.skill_discover_status or ""),
                "skills": str(repository.skill_num),
                "platform": repository.platform or "-",
                "branch": repository.branch or "-",
                "name": _display_skill_repo_name(repository),
                "url": repository.url or "-",
            }
        )

    _print_table(
        rows,
        [
            ("id", "repo_id", 36),
            ("status", "repo_status", 16),
            ("skills", "skill_count", 11),
            ("platform", "platform", 12),
            ("branch", "branch", 18),
            ("name", "repo_name", 36),
            ("url", "repo_url", 80),
        ],
        empty_message="No skill repos found.",
    )

    counts = _status_counts(repositories)
    summary = ", ".join(f"{status}={count}" for status, count in counts.items())
    print()
    print(f"Total: {len(repositories)} skill repos")
    print(f"Statuses: {summary}")


async def _run_query(manager: "SkillManager", args: argparse.Namespace) -> int:
    if args.id:
        repository = await manager.get_repository_by_id(args.id)
        logger.info("%s", _format_skill_repo(repository))
        return 0

    repositories = await manager.list_skill_repositories()
    _print_repository_table(repositories)
    return 0


async def _discover_repositories_from_requests(
    manager: "SkillManager",
    requests: list[SkillRepositoryRequest],
    *,
    source_label: str = "",
    config_keys: list[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """逐仓库执行 discover，输出表格，返回 (退出码, 本轮运行数据)。

    运行数据供 `_run_discover` 在补跑衍生字段缺口后构建结果 JSON（与自动
    调度输出完全对齐）。
    """
    total = len(requests)
    counters = {
        "created": 0,
        "rediscovered": 0,
        "unchanged": 0,
        "removed": 0,
        "no_skill": 0,
        "failed": 0,
    }
    fatal_error = False
    result_rows: list[dict[str, str]] = []
    failure_details: list[dict[str, str]] = []
    repo_details: list[dict[str, Any]] = []
    run_started = time.monotonic()
    started_at = datetime.now()

    logger.info("[Discover] Preparing to discover %d repos%s for SKILL.md", total, source_label)

    for index, request in enumerate(requests, start=1):
        label = request.url or "<missing-url>"
        logger.info("[Discover] [%d/%d] scanning %s", index, total, label)
        repo_started = time.monotonic()

        try:
            result = await manager.discover_configured_skill_repository(request)
        except Exception as exc:
            elapsed = time.monotonic() - repo_started
            await manager.rollback()
            counters["failed"] += 1
            fatal_error = isinstance(exc, CategoryClassificationError)
            error = _format_exception(exc)
            repo_details.append(
                {
                    "url": label,
                    "outcome": "failed",
                    "skill_num": 0,
                    "duration_seconds": round(elapsed, 3),
                    "error": error,
                }
            )
            _print_failure_detail(str(index), label, error)
            result_rows.append(
                {
                    "#": str(index),
                    "result": "failed",
                    "status": "-",
                    "skills": "-",
                    "name": "-",
                    "url": label,
                    "error": error,
                }
            )
            failure_details.append(
                {
                    "#": str(index),
                    "url": label,
                    "error": error,
                }
            )
            if fatal_error:
                break
            continue

        elapsed = time.monotonic() - repo_started
        # outcome 口径与自动调度共用（classify_discover_result）
        outcome = classify_discover_result(result)
        counters[outcome] += 1
        repo_details.append(
            {
                "url": label,
                "outcome": outcome,
                "skill_num": getattr(result, "skill_num", 0) or 0,
                "duration_seconds": round(elapsed, 3),
            }
        )

        if outcome == "no_skill":
            result_rows.append(
                {
                    "#": str(index),
                    "result": "no_skill",
                    "status": "-",
                    "skills": "0",
                    "name": "-",
                    "url": label,
                    "error": "SKILL.md not found",
                }
            )
        elif outcome == "removed":
            result_rows.append(
                {
                    "#": str(index),
                    "result": "removed",
                    "status": "deleted",
                    "skills": str(result.skill_num),
                    "name": _display_skill_repo_name(result),
                    "url": result.url or label,
                    "error": "SKILL.md not found",
                }
            )
        elif outcome == "unchanged":
            result_rows.append(
                {
                    "#": str(index),
                    "result": "unchanged",
                    "status": str(result.skill_discover_status or "-"),
                    "skills": str(result.skill_num),
                    "name": _display_skill_repo_name(result),
                    "url": result.url or label,
                    "error": "commit unchanged",
                }
            )
        else:
            result_rows.append(
                {
                    "#": str(index),
                    "result": outcome,
                    "status": str(result.skill_discover_status or "-"),
                    "skills": str(result.skill_num),
                    "name": _display_skill_repo_name(result),
                    "url": result.url or label,
                    "error": "-",
                }
            )

    print()
    _print_table(
        result_rows,
        DISCOVER_RESULT_COLUMNS,
        empty_message="No repos discovered.",
    )
    _print_failure_details(failure_details)
    print()
    logger.info(
        "[Discover] Discover summary: total=%d created=%d rediscovered=%d "
        "unchanged=%d removed=%d no_skill=%d failed=%d",
        total,
        counters["created"],
        counters["rediscovered"],
        counters["unchanged"],
        counters["removed"],
        counters["no_skill"],
        counters["failed"],
    )

    run_data = {
        "started_at": started_at,
        "run_started": run_started,
        "total_repos": total,
        "config_keys": list(config_keys or []),
        "counters": counters,
        "repo_details": repo_details,
    }
    return (1 if counters["failed"] else 0), run_data
# ============ discover 结果 JSON（CLI 与自动调度共用，口径完全一致） ============

# counters 各项含义，随结果文件内嵌为 <key>_note，便于离线解读（与设计文档 §11 口径一致）
# skill_stats / remediation_run 各项含义，同样以 <key>_note 内嵌输出（与设计文档 §11 口径一致）
# remediation_run 三个补齐任务各自的 <key>_note 口径
# security_audits 中「异步检测仍在进行」的判定条件（JSONB 字段，无外键，
# resource_type='skill' + resource_id 关联 skills.id / skill_versions.id）
# 安全检测失败 skill_id 列表的输出上限，避免结果文件过大
async def _run_remediations_safe() -> dict[str, Any] | None:
    """扫描结束后补跑衍生字段缺口（security/category/embedding），返回本轮结果。

    统一入口见 ``src/jobs/remediation.py``：各任务自持 pg_advisory_lock
    互斥（embedding 的锁另与全量重建脚本互斥），单任务失败/跳过只记录在
    该任务的统计里。失败不重试，漏掉的行由下轮 discover 或 API 侧的
    低频补齐循环自愈。
    """
    from src.jobs.remediation import run_remediations

    try:
        results = await run_remediations()
        logger.info("[Discover] Remediation after discover: %s", results)
        return results
    except Exception:
        logger.exception("[Discover] Remediation after discover failed")
        return None


async def _collect_skill_stats_safe() -> dict[str, Any] | None:
    """采集 skill 级统计；查询失败返回 None，不影响主流程。"""
    try:
        from src.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            return await collect_skill_stats(session)
    except Exception:
        logger.exception("[Discover] Failed to collect skill stats after discover")
        return None


async def _run_discover(
    manager: "SkillManager",
    args: argparse.Namespace,
) -> int:
    """执行一轮 discover：扫描 → 补跑衍生字段缺口 → 采集统计 → 落结果 JSON。

    结果文件结构与自动调度完全一致（build_discover_result_document 共用）。
    """
    if args.url:
        exit_code, run_data = await _run_discover_single_skill_yaml(manager, args)
    else:
        exit_code, run_data = await _run_discover_from_config(manager, args)
    remediation_run = await _run_remediations_safe()
    skill_stats = await _collect_skill_stats_safe()
    save_discover_result_file(
        build_discover_result_document(
            started_at=run_data["started_at"],
            run_started=run_data["run_started"],
            total_repos=run_data["total_repos"],
            config_keys=run_data["config_keys"],
            counters=run_data["counters"],
            repo_details=run_data["repo_details"],
            remediation_run=remediation_run,
            skill_stats=skill_stats,
        )
    )
    return exit_code


async def _run_discover_single_skill_yaml(
    manager: "SkillManager",
    args: argparse.Namespace,
) -> tuple[int, dict[str, Any]]:
    """--url 单文件模式：只扫描指定 skill.yaml 登记的 skill_repos / skills[]。"""
    requests = _build_requests_from_skill_yaml(args.url)
    return await _discover_repositories_from_requests(
        manager,
        requests,
        source_label=f" from {args.url}",
        config_keys=[args.url],
    )


async def _run_discover_from_config(
    manager: "SkillManager",
    args: argparse.Namespace,
) -> tuple[int, dict[str, Any]]:
    # 统一解析 catalog 路径（未指定 --repository-path 时自动同步一次），
    # 供请求构建与路径打印共用，避免重复 sync
    repository_path = (
        Path(args.repository_path).expanduser()
        if args.repository_path
        else sync_openEuler_skills_repo()
    )
    requests, config_keys = build_configured_discover_requests(
        platform=args.platform,
        repository_path=repository_path,
    )
    return await _discover_repositories_from_requests(
        manager,
        requests,
        source_label=f" from {', '.join(config_keys)} (catalog: {repository_path})",
        config_keys=config_keys,
    )


async def _run_delete(manager: "SkillManager", args: argparse.Namespace) -> int:
    if not args.id:
        raise _build_cli_error(
            "delete",
            "--id is required for delete",
            "python main.py delete --id <repo_id>",
        )
    await manager.delete_skill_repository(args.id)
    logger.info("[Discover] Deleted skill repo: id=%s", args.id)
    return 0


POPULARITY_RESULT_COLUMNS = [
    ("#", "#", 5),
    ("source", "source", 10),
    ("type", "type", 12),
    ("stars", "stars", 10),
    ("forks", "forks", 10),
    ("watchers", "watchers", 12),
    ("est_downloads", "est.downloads", 14),
    ("url", "url", 64),
]


async def _run_popularity(
    manager: "SkillManager",
    args: argparse.Namespace,
) -> int:
    """Collect popularity (stars/forks/watchers) for configured repos.

    Fetches metrics from the hosting platform API, stores them on the
    matching ``skill_repos`` row (matched by URL), then distributes each
    repository's estimated total downloads across its skills. Repos are
    weighted by type (enterprise / community / personal) and individual
    skills by category popularity and risk score, so every skill ends up
    with a different download count. The frontend ranks popularity by the
    existing ``download_count`` field, so no frontend changes are needed.
    """
    collector = PopularityCollector()
    results = await collector.collect(None, only=args.source)

    if not results:
        logger.info("[Discover] No repos to collect popularity for")
        return 0

    saved = 0
    not_found = 0
    skills_updated = 0
    rows: list[dict[str, str]] = []
    for index, popularity in enumerate(results, start=1):
        repo_downloads = estimate_repo_downloads(
            popularity.stars, popularity.forks, popularity.watchers, popularity.repo_type
        )
        rows.append(
            {
                "#": str(index),
                "source": popularity.source,
                "type": popularity.repo_type,
                "stars": str(popularity.stars),
                "forks": str(popularity.forks),
                "watchers": str(popularity.watchers),
                "est_downloads": str(repo_downloads),
                "url": popularity.url,
            }
        )
        repository = await manager.skill_repo_repository.get_skill_repository_by_url(
            popularity.url
        )
        if repository is None:
            not_found += 1
            logger.info(
                "[Discover] No skill_repos row for %s (URL not registered); skip persist",
                popularity.url,
            )
            continue
        await manager.skill_repo_repository.update_skill_repository(
            repository.id,
            stars_count=popularity.stars,
            forks_count=popularity.forks,
            watchers_count=popularity.watchers,
            popularity_updated_at=datetime.now(timezone.utc),
        )
        skills = await manager.skill_repository.list_by_skill_repo_id(repository.id)
        allocations = allocate_skill_downloads(repo_downloads, skills)
        for skill_id, download_count in allocations.items():
            await manager.skill_repository.update_download_count_by_skill_id(
                skill_id, download_count
            )
        skills_updated += len(allocations)
        saved += 1

    print()
    _print_table(
        rows,
        POPULARITY_RESULT_COLUMNS,
        empty_message="No popularity results.",
    )
    print()
    logger.info(
        "[Discover] Popularity summary: total=%d saved=%d not_registered=%d skills_updated=%d",
        len(results), saved, not_found, skills_updated,
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage skill repository discovery.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    _add_query_parser(subparsers)
    _add_discover_parser(subparsers)
    _add_delete_parser(subparsers)
    _add_popularity_parser(subparsers)
    return parser


def _add_query_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "query",
        help="Query skill repo records",
        description="Query skill repo records.",
    )
    parser.add_argument("-i", "--id", help="Skill repo ID to look up")


def _add_discover_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "discover",
        help="Register, clone, and scan skills from config or a single skill.yaml",
    )
    parser.add_argument(
        "-u",
        "--url",
        help=(
            "Single skill.yaml to discover: a local file path, or a path "
            "relative to the openEuler-skills catalog (e.g. "
            "personal/<owner>/skill.yaml); scans only its skill_repos / "
            "skills[] entries"
        ),
    )
    parser.add_argument(
        "-r",
        "--repository-path",
        default=None,
        help="Local openEuler-skills checkout (default: auto-download)",
    )
    parser.add_argument(
        "-p",
        "--platform",
        choices=PLATFORM_CHOICES,
        default=None,
        help="Config repo list to read (default: all repo lists)",
    )


def _add_delete_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("delete", help="Delete a skill repo")
    parser.add_argument("-i", "--id", required=True, help="Skill repo ID to delete")


def _add_popularity_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "popularity",
        help="Collect popularity (stars/forks/watchers) for configured repos",
        description=(
            "Query GitHub/AtomGit public APIs for repository popularity metrics "
            "and persist them on the matching skill_repos rows."
        ),
    )
    parser.add_argument(
        "-s",
        "--source",
        choices=("github", "atomgit"),
        default=None,
        help="Only collect from this platform (default: all)",
    )


async def _main() -> int:
    args = _build_parser().parse_args()

    # Resolve the catalog once for discover so the classifier and request
    # builder share the same checkout and do not issue duplicate pulls.
    if args.action == "discover" and not getattr(args, "repository_path", None):
        args.repository_path = str(sync_openEuler_skills_repo())

    async with get_db_context() as session:
        skill_repository = SkillRepository(session)
        skill_repo_repository = SkillRepoRepository(session)
        manager = SkillManager(
            skill_repository=skill_repository,
            skill_repo_repository=skill_repo_repository,
            contributor_repository=ContributorRepository(session),
            catalog_path=(Path(args.repository_path).expanduser() if getattr(args, "repository_path", None) else None),
        )

        if args.action == "query":
            return await _run_query(manager, args)
        if args.action == "discover":
            return await _run_discover(manager, args)
        if args.action == "delete":
            return await _run_delete(manager, args)
        if args.action == "popularity":
            return await _run_popularity(manager, args)
    raise ValueError(f"Unsupported action: {args.action}")


def main() -> int:
    """Run the async skill discovery CLI from a console-script or Python process."""
    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        logger.warning("Canceled")
        return 130
    except Exception as exc:
        logger.error("%s", _format_exception(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
