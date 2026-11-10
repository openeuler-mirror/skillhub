from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from src.utils.git_operations import GitOperations

OPENEULER_SKILLS_URL = "https://atomgit.com/openeuler/openEuler-skills"

# catalog 的三个平台渠道（community/enterprise/personal）统一在此定义，
# 供本模块与 skill_manager / discover_requests 引用，避免多处硬编码不同步。
CATALOG_PLATFORMS = ("personal", "community", "enterprise")

logger = logging.getLogger(__name__)


def sync_openEuler_skills_repo() -> Path:
    """Return the local openEuler-skills checkout, cloning it when absent."""
    from src.core.config import get_settings
    local_path = Path(get_settings().storage.local_path) / "skill-repositories" / "openEuler-skills"
    openEuler_skills_path = local_path.expanduser()
    openEuler_skills_path.parent.mkdir(parents=True, exist_ok=True)
    GitOperations().sync_catalog_repository(openEuler_skills_path, OPENEULER_SKILLS_URL)
    return openEuler_skills_path


def _iter_catalog_entries(path: Path) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Single-pass walk over the catalog, yielding ``(platform, owner, entry)``.

    ``platform`` is one of ``community`` / ``enterprise`` / ``personal`` and
    ``owner`` the directory name under it. Malformed YAML files are skipped
    with a warning instead of aborting the whole read.
    """
    for platform in CATALOG_PLATFORMS:
        root = path / platform
        if not root.is_dir():
            continue
        for skill_file in sorted(root.glob("*/skill.yaml")):
            try:
                with skill_file.open("r", encoding="utf-8") as fh:
                    entry = yaml.safe_load(fh) or {}
            except yaml.YAMLError as exc:
                logger.warning("Skip malformed catalog file %s: %s", skill_file, exc)
                continue
            if isinstance(entry, dict):
                yield platform, skill_file.parent.name, entry


def normalize_dict_keys(data: Any) -> Any:
    if isinstance(data, dict):
        return {
            str(key).strip(): normalize_dict_keys(value)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [normalize_dict_keys(item) for item in data]
    return data


def load_crawler_config(repository_path: Path | None = None) -> dict[str, Any]:
    """Load crawler URLs from an openEuler-skills catalog repository.

    A directory is treated as an openEuler-skills checkout. Each
    ``community/<sig>/skill.yaml``, ``enterprise/<company>/skill.yaml`` and
    ``personal/<person>/skill.yaml`` contributes its ``skill_repos`` entries.
    YAML files are not accepted as a source.
    """
    path = (repository_path or sync_openEuler_skills_repo()).expanduser()
    if not path.is_dir():
        raise ValueError(f"Catalog path must be a repository directory: {path}")

    result: dict[str, Any] = {platform: [] for platform in CATALOG_PLATFORMS}
    for platform, owner, entry in _iter_catalog_entries(path):
        repos = entry.get("skill_repos", [])
        if not isinstance(repos, list):
            continue
        for repo in repos:
            if not isinstance(repo, dict):
                result[platform].append(repo)
                continue
            enriched = dict(repo)
            if platform == "community":
                enriched.setdefault("sig_name", owner)
            result[platform].append(enriched)
    normalized = normalize_dict_keys(result)
    if not isinstance(normalized, dict):
        raise ValueError(f"Invalid config format in {path}")
    return normalized


def load_catalog_skill_url_entries(repository_path: Path | None = None) -> list[dict[str, Any]]:
    """收集 catalog 中的单 Skill URL 贡献条目（``skills[].skill_url``）。

    返回 ``{"platform", "owner", "skill_name", "skill_url"}`` 列表，仅做收集；
    URL 解析与字段校验（skill_name / skill_url 必填）在请求构建侧完成。
    """
    path = (repository_path or sync_openEuler_skills_repo()).expanduser()
    entries: list[dict[str, Any]] = []
    for platform, owner, entry in _iter_catalog_entries(path):
        skills = entry.get("skills")
        if not isinstance(skills, list):
            continue
        for skill in skills:
            if not isinstance(skill, dict):
                continue
            entries.append({
                "platform": platform,
                "owner": owner,
                "skill_name": skill.get("skill_name"),
                "skill_url": skill.get("skill_url"),
            })
    return entries


def load_catalog_local_skill_entries(repository_path: Path | None = None) -> list[dict[str, Any]]:
    """收集 catalog 中的原文件方式贡献条目（``local_skills``）。

    返回 ``{"platform", "owner", "author", "skill_name", "skill_path"}`` 列表。
    ``skill_path`` 为仓库根目录 ``skills/<skill_name>/SKILL.md`` 相对路径；
    ``author`` 口径与 ``load_contributor_profiles`` 一致（enterprise 取
    organization 字段否则 owner，community/personal 取 owner）。
    """
    path = (repository_path or sync_openEuler_skills_repo()).expanduser()
    entries: list[dict[str, Any]] = []
    for platform, owner, entry in _iter_catalog_entries(path):
        local_skills = entry.get("local_skills")
        if not isinstance(local_skills, list):
            continue
        if platform == "enterprise":
            author = _clean_optional_str(entry.get("organization")) or owner
        else:
            author = owner
        for skill_name in local_skills:
            if not isinstance(skill_name, str):
                continue
            name = skill_name.strip()
            if not name:
                continue
            entries.append({
                "platform": platform,
                "owner": owner,
                "author": author,
                "skill_name": name,
                "skill_path": f"skills/{name}/SKILL.md",
            })
    return entries


def load_single_catalog_entry(skill_yaml: Path) -> tuple[str, str, dict[str, Any]]:
    """读取单个 skill.yaml → ``(platform, owner, entry)``（``--url`` 单文件模式）。

    platform / owner 取自文件所在目录结构
    ``<catalog>/<platform>/<owner>/skill.yaml``，与全量扫描的
    ``_iter_catalog_entries`` 口径一致；文件名必须恰好是 ``skill.yaml``。
    文件不存在、目录结构不符或 YAML 损坏时抛 ``ValueError``。
    """
    path = skill_yaml.expanduser()
    if not path.is_file():
        raise ValueError(f"skill.yaml not found: {skill_yaml}")
    if path.name != "skill.yaml":
        raise ValueError(f"expected a skill.yaml file: {skill_yaml}")
    owner = path.parent.name
    platform = path.parent.parent.name
    if platform not in CATALOG_PLATFORMS or not owner:
        raise ValueError(
            "skill.yaml must live at <catalog>/<platform>/<owner>/skill.yaml: "
            f"{skill_yaml}"
        )
    try:
        with path.open("r", encoding="utf-8") as fh:
            entry = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid skill.yaml format: {skill_yaml}: {exc}") from exc
    if not isinstance(entry, dict):
        raise ValueError(f"Invalid skill.yaml format: {skill_yaml}")
    return platform, owner, entry


def _first_repo_url(entry: dict[str, Any]) -> str | None:
    """First registered git URL: skill_repos[].url, else skills[].skill_url prefix."""
    repos = entry.get("skill_repos")
    if isinstance(repos, list):
        for repo in repos:
            if isinstance(repo, dict) and isinstance(repo.get("url"), str) and repo["url"].strip():
                return repo["url"].strip()
    skills = entry.get("skills")
    if isinstance(skills, list):
        for skill in skills:
            if not isinstance(skill, dict):
                continue
            skill_url = skill.get("skill_url")
            if isinstance(skill_url, str) and skill_url.strip():
                # <host>/<owner>/<repo>/blob/<ref>/<path>/SKILL.md -> repo URL
                return skill_url.split("/blob/", 1)[0].strip()
    return None


def _extract_contributor_profile(platform: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Extract contributor profile fields per platform template spec.

    git_profile 是代码托管平台主页（git_profile）；
    website是官网地址（community 的 SIG 页面 / 企业官网 / 个人网站）。

    community  (templates/community-skill-spec.yaml):  author /
               description / website（均为一级字段；无 git 主页字段）
    enterprise (templates/enterprise-skill-spec.yaml): organization /
               display_name / git_profile / website / description /
               public_email / location（均为一级字段）
    personal   (templates/personal-skill-spec.yaml):   author /
               description / git_profile / website（均为一级字段）
    """
    if platform == "community":
        return {
            "name": entry.get("author"),
            "description": entry.get("description"),
            "git_profile": None,
            "website": entry.get("website"),
        }
    if platform == "enterprise":
        return {
            "name": entry.get("display_name"),
            "description": entry.get("description"),
            "git_profile": entry.get("git_profile"),
            "website": entry.get("website"),
        }
    if platform == "personal":
        return {
            "name": entry.get("author"),
            "description": entry.get("description"),
            "git_profile": entry.get("git_profile"),
            "website": entry.get("website"),
        }
    return {"name": None, "description": None, "git_profile": None, "website": None}


def _clean_optional_str(value: Any) -> str | None:
    """Normalize yaml "null"/empty values to None."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def load_contributor_profiles(repository_path: Path | None = None) -> list[dict[str, Any]]:
    """Read contributor profiles from the openEuler-skills catalog.

    Walks ``community/<sig>/``, ``enterprise/<org>/`` and ``personal/<author>/``
    ``skill.yaml`` files and returns one profile dict per directory:
    ``source`` (git host derived from the first registered repo URL),
    ``author`` (catalog identity; the ``organization`` field for enterprise,
    the directory name otherwise), ``platform``,
    ``name`` / ``description`` / ``git_profile`` (git hosting profile) /
    ``website`` (official site) and ``repo_url`` (first registered repo URL).
    """
    from src.skills.skill_parser import derive_skill_source

    path = (repository_path or sync_openEuler_skills_repo()).expanduser()
    profiles: list[dict[str, Any]] = []
    for platform, owner, entry in _iter_catalog_entries(path):
        if platform == "enterprise":
            author = _clean_optional_str(entry.get("organization")) or owner
        else:
            author = owner
        repo_url = _first_repo_url(entry)
        source = None
        if repo_url:
            try:
                source, _ = derive_skill_source(repo_url)
            except ValueError:
                source = None
        profile = _extract_contributor_profile(platform, entry)
        profiles.append(
            {
                "source": source,
                "author": author,
                "platform": platform,
                "name": _clean_optional_str(profile["name"]),
                "description": _clean_optional_str(profile["description"]),
                "git_profile": _clean_optional_str(profile["git_profile"]),
                "website": _clean_optional_str(profile["website"]),
                "repo_url": repo_url,
            }
        )
    return profiles
