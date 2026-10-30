"""discover 请求构建：从 openEuler-skills catalog 解析出 SkillRepositoryRequest 列表。

供 CLI discover 与后台定时调度共用（skillcrawler/main.py 与
src/jobs/discover_scheduler.py 均从此导入），与结果输出
（src/skills/discover_result.py）分属两个关注点。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.skills.config import (
    load_catalog_skill_url_entries,
    load_crawler_config,
    load_single_catalog_entry,
    sync_openEuler_skills_repo,
)
from src.skills.skill_manager import SkillRepositoryRequest
from src.skills.skill_parser import normalize_git_clone_url, parse_skill_md_url

logger = logging.getLogger(__name__)

PLATFORM_CHOICES = ("personal", "community", "enterprise")


def _normalize_platform(platform: str) -> str:
    normalized = platform.strip().lower()
    if normalized not in PLATFORM_CHOICES:
        raise ValueError(
            f"Unsupported platform {platform!r}; expected community, enterprise, or personal"
        )
    return normalized


def _build_requests_from_config(
    repos_dict: dict[str, Any],
    platform: str,
    seen_urls: set[str] | None = None,
) -> list[SkillRepositoryRequest]:
    """Build requests for one catalog group from an already-loaded catalog."""
    platform_urls = repos_dict.get(platform)
    if not isinstance(platform_urls, list) or not platform_urls:
        logger.warning(f"[Discover] No {platform} entries found in config")
        return []

    seen = seen_urls if seen_urls is not None else set()
    requests: list[SkillRepositoryRequest] = []
    for item in platform_urls:
        if not isinstance(item, dict):
            raise ValueError(f"Invalid entry in {platform}: {item!r}")
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"Invalid url entry in {platform}: {item!r}")
        url = url.strip()
        if url in seen:
            continue
        seen.add(url)
        branch = item.get("branch") or None
        sig_name = item.get("sig_name") if platform == "community" else None
        if sig_name is not None:
            sig_name = str(sig_name).strip() or None
        requests.append(
            SkillRepositoryRequest(
                url=url,
                branch=branch,
                platform=platform,
                sig_name=sig_name,
            )
        )
    return requests


def _build_requests_from_skill_urls(
    entries: list[dict[str, Any]],
    platform_keys: list[str],
    covered_urls: set[str],
) -> list[SkillRepositoryRequest]:
    """把 catalog 的 skills[].skill_url 单 Skill 贡献合并为收窄扫描请求。

    - 按规范化 repo URL 分组：同一仓库多条贡献合并为一个请求、路径取并集
      （部分扫描按请求同步入库，拆成多个请求会互相删掉对方的 skill）；
    - 同一仓库的多条贡献必须同 ref（/blob/ 后第一段，约定为分支名），
      否则无法消歧，抛 ValueError；
    - 仓库已被 skill_repos 整仓登记（covered_urls 命中）时跳过，整仓扫描
      已覆盖该 skill；
    - skill_name / skill_url 均必填（openEuler-skills README 的 URL 贡献约定）。
    """
    covered = {normalize_git_clone_url(url) for url in covered_urls}
    grouped: dict[str, dict[str, Any]] = {}
    for item in entries:
        platform = str(item.get("platform") or "").strip()
        if platform not in platform_keys:
            continue
        skill_name = str(item.get("skill_name") or "").strip()
        skill_url = str(item.get("skill_url") or "").strip()
        if not skill_name or not skill_url:
            raise ValueError(
                f"Invalid skills entry in {platform} (skill_name and skill_url "
                f"are required): {item!r}"
            )
        repo_url, ref, skill_path = parse_skill_md_url(skill_url)
        if repo_url in covered:
            continue
        group = grouped.get(repo_url)
        if group is None:
            grouped[repo_url] = {
                "branch": ref,
                "platform": platform,
                "sig_name": str(item.get("owner") or "").strip()
                if platform == "community"
                else None,
                "skill_paths": {skill_path},
            }
        elif group["branch"] != ref:
            raise ValueError(
                f"skill_url entries for {repo_url} must share one ref, "
                f"got {group['branch']!r} and {ref!r}"
            )
        else:
            group["skill_paths"].add(skill_path)

    return [
        SkillRepositoryRequest(
            url=repo_url,
            branch=group["branch"],
            platform=group["platform"],
            sig_name=group["sig_name"],
            skill_paths=sorted(group["skill_paths"]),
        )
        for repo_url, group in grouped.items()
    ]


def _build_requests_from_skill_yaml(skill_yaml: str) -> list[SkillRepositoryRequest]:
    """从单个 skill.yaml 构建 discover 请求（``--url`` 单文件模式）。

    与全量构建同口径：``skill_repos`` 整仓条目 + ``skills[].skill_url`` 单
    Skill 收窄请求（整仓登记覆盖同文件的 skills[] 条目）；platform / owner
    取自文件目录结构（不再由 URL 推断）。路径先按本地文件解析，不存在时按
    catalog 根（``-r`` / 自动同步）解析。
    """
    resolved = Path(skill_yaml).expanduser()
    if not resolved.is_file():
        resolved = (sync_openEuler_skills_repo() / skill_yaml).expanduser()
    platform, owner, entry = load_single_catalog_entry(resolved)

    seen_urls: set[str] = set()
    repos = entry.get("skill_repos", [])
    enriched: list[Any] = []
    if isinstance(repos, list):
        for repo in repos:
            if isinstance(repo, dict):
                repo = dict(repo)
                if platform == "community":
                    repo.setdefault("sig_name", owner)
            enriched.append(repo)
    requests = _build_requests_from_config({platform: enriched}, platform, seen_urls)

    skill_entries = [
        {
            "platform": platform,
            "owner": owner,
            "skill_name": skill.get("skill_name"),
            "skill_url": skill.get("skill_url"),
        }
        for skill in (entry.get("skills") or [])
        if isinstance(skill, dict)
    ]
    requests.extend(_build_requests_from_skill_urls(skill_entries, [platform], seen_urls))
    return requests


def build_configured_discover_requests(
    platform: str | None = None,
    repository_path: Path | None = None,
) -> tuple[list[SkillRepositoryRequest], list[str]]:
    """从 openEuler-skills catalog（或显式覆盖配置）构建 discover 请求列表。

    供 CLI discover 与后台定时调度共用：读取全部三个 key
    （community / personal / enterprise）并按 URL 去重。
    指定 platform 时仅读取对应 key 且要求非空；否则每个 key 都允许为空。
    """
    # The default source is the openEuler-skills catalog repository.
    if repository_path is not None:
        repository_path = repository_path.expanduser()
        if not repository_path.exists():
            raise ValueError(f"openEuler-skills repository path does not exist: {repository_path}")
        if not repository_path.is_dir():
            raise ValueError(f"openEuler-skills repository path is not a directory: {repository_path}")
    else:
        repository_path = sync_openEuler_skills_repo()
    repos_dict = load_crawler_config(repository_path)

    if platform is None:
        platform_keys = list(PLATFORM_CHOICES)
    else:
        platform_keys = [_normalize_platform(platform)]

    requests: list[SkillRepositoryRequest] = []
    seen_urls: set[str] = set()
    for platform in platform_keys:
        requests.extend(
            _build_requests_from_config(
                repos_dict,
                platform,
                seen_urls,
            )
        )

    # URL 方式贡献（skills[].skill_url）：整仓登记覆盖不到的单 Skill，
    # 按仓库分组合并为收窄扫描请求（skill_paths 只收录登记的 SKILL.md）
    requests.extend(
        _build_requests_from_skill_urls(
            load_catalog_skill_url_entries(repository_path),
            platform_keys,
            seen_urls,
        )
    )

    return requests, platform_keys
