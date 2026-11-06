"""Skill scanning logic: version scan, current state scan, and record building."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.ai.category_classifier import DeepSeekCategoryClassifier
from src.utils.git_operations import GitOperations
from src.core.categories import CANONICAL_CATEGORIES
from src.security.audit_trigger import trigger_existing_skill_audit
from src.security.detector import SecurityDetector, SecurityReport
from src.skills.skill_parser import (
    as_optional_str,
    as_optional_str_list,
    build_skill_md_url,
    derive_repository_skill_name,
    derive_skill_source,
    extract_declared_tags,
    extract_declared_version,
    is_incomplete_skill_metadata,
    is_low_value_skill_content,
)
from src.models.orm import Skill, SkillVersion
from src.models.repository import SkillRepository

from src.skills.skill_id import build_skill_id, extract_owner_repo

if TYPE_CHECKING:
    from src.models.orm import SkillRepoModel

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SecurityResolution:
    risk_score: int | None
    audit_details: dict[str, Any] | None
    audit_triggered: bool
    # async 审计已发起（无论触发成功与否），用于失败时也落 unknown 审计记录
    audit_attempted: bool = False


class SkillScanner:
    """Scans repositories for skills and builds skill records."""

    def __init__(
        self,
        git_ops: GitOperations,
        skill_repository: SkillRepository,
        category_classifier: DeepSeekCategoryClassifier | None = None,
        security_detector: SecurityDetector | None = None,
        security_async_mode: bool = False,
    ) -> None:
        self.git_ops = git_ops
        self.skill_repository = skill_repository
        self.category_classifier = category_classifier
        self.security_detector = security_detector
        self.security_async_mode = security_async_mode
        # Per-scan security cache statistics (reset at the start of each scan_repo)
        self._security_cache_hits: int = 0
        self._security_audit_submitted: int = 0
        self._security_audit_pending: int = 0

    # ── Version snapshots ──────────────────────────────────────────

    @staticmethod
    def _build_version_snapshots(
        repository_git_metadata: dict[str, Any],
    ) -> list[dict[str, str]]:
        """Build tag version snapshots from raw git metadata.

        Pure transformation: Tag list + commit-id map → snapshot dicts.
        No git commands are executed here.
        """
        latest_tags = as_optional_str_list(
            repository_git_metadata.get('latest_tags'),
        ) or []
        latest_tag_commits = repository_git_metadata.get('latest_tag_commits') or {}
        snapshots: list[dict[str, str]] = []

        for tag in latest_tags:
            commit_id = as_optional_str(latest_tag_commits.get(tag))
            if commit_id is None:
                continue
            snapshots.append({
                'ref': tag,
                'version': tag,
                'commit_id': commit_id,
                'version_source': 'tag',
            })

        return snapshots

    async def start_scan(
        self,
        repo: SkillRepoModel,
        repo_root: Path,
        repository_git_metadata: dict[str, Any] | None = None,
        author: str | None = None,
        skill_paths: list[str] | None = None,
        skill_path_filter: set[str] | None = None,
        skill_authors: dict[str, str] | None = None,
        skill_platforms: dict[str, str] | None = None,
    ) -> tuple[list[Skill], list[SkillVersion]]:
        self._security_cache_hits = 0
        self._security_audit_submitted = 0
        self._security_audit_pending = 0
        if not repo_root.exists():
            raise ValueError(
                f'Repository root does not exist for repository {repo.id}: {repo_root}'
            )

        discovery_started_at = time.perf_counter()
        if skill_paths is None:
            skill_paths = await asyncio.to_thread(
                self.git_ops.list_skill_paths_for_ref, repo_root, 'HEAD',
            )
        _logger.debug(
            '[Discover] Skill file discovery timing: repo=%s files=%d elapsed=%.3fs',
            getattr(repo, 'repo_name', None) or repo.url,
            len(skill_paths),
            time.perf_counter() - discovery_started_at,
        )

        repository_git_metadata = repository_git_metadata or {}
        repository_commit_id = as_optional_str(repository_git_metadata.get('commit_id'))
        repository_latest_tags = as_optional_str_list(repository_git_metadata.get('latest_tags')) or []
        existing_skills, existing_versions = await self.skill_repository.load_scan_records(repo.id)
        category_cache = {
            skill_id: skill.category
            for skill_id, skill in existing_skills.items()
            if getattr(skill, 'category', None)
        }
        # Cross-version security result cache keyed by tree_hash: a tagged
        # version whose content is identical to the already-audited latest
        # (or an earlier tag) reuses the result instead of re-submitting.
        security_cache: dict[str, SecurityResolution] = {}
        latest_skills, metadata_version_rows = await self._scan_latest_skills(
            repo=repo,
            repo_root=repo_root,
            skill_paths=skill_paths,
            repository_commit_id=repository_commit_id,
            repository_latest_tags=repository_latest_tags,
            author=author,
            existing_skills=existing_skills,
            existing_versions=existing_versions,
            category_cache=category_cache,
            security_cache=security_cache,
            skill_authors=skill_authors,
            skill_platforms=skill_platforms,
        )

        tagged_skills: list[SkillVersion] = []
        version_snapshots = self._build_version_snapshots(repository_git_metadata)
        if version_snapshots:
            tagged_skills = await self._scan_tagged_skills(
                repo=repo,
                repo_root=repo_root,
                version_snapshots=version_snapshots,
                repository_commit_id=repository_commit_id,
                repository_latest_tags=repository_latest_tags,
                author=author,
                existing_versions=existing_versions,
                category_cache=category_cache,
                security_cache=security_cache,
                skill_path_filter=skill_path_filter,
                skill_authors=skill_authors,
                skill_platforms=skill_platforms,
                # HEAD 声明了 frontmatter version 的 skill 自管版本，
                # 完全跳过 tag 扫描（不再以 tag 作为版本来源）。
                skip_skill_ids={row.skill_id for row in metadata_version_rows},
            )

        return latest_skills, [*metadata_version_rows, *tagged_skills]

    async def _scan_latest_skills(
        self,
        repo: SkillRepoModel,
        repo_root: Path,
        skill_paths: list[str],
        repository_commit_id: str | None,
        repository_latest_tags: list[str],
        author: str | None,
        existing_skills: dict[str, Skill],
        existing_versions: dict[tuple[str, str | None], SkillVersion],
        category_cache: dict[str, str | None],
        security_cache: dict[str, SecurityResolution] | None = None,
        skill_authors: dict[str, str] | None = None,
        skill_platforms: dict[str, str] | None = None,
    ) -> tuple[list[Skill], list[SkillVersion]]:
        discovered: list[Skill] = []
        metadata_version_rows: list[SkillVersion] = []
        # skill_id is derived from the SKILL.md directory name, so different
        # paths with the same directory name collide.  Keep the first one so
        # every returned record maps to a persisted row (records dropped here
        # would never receive a database id).
        seen_skill_ids: set[str] = set()
        tree_hashes = await asyncio.to_thread(
            self.git_ops.get_skill_tree_hashes,
            repo_root, skill_paths, ref='HEAD',
        )
        for relative_path in skill_paths:
            scan_started_at = time.perf_counter()
            skill_id = build_skill_id(
                repo.source, extract_owner_repo(repo.url), relative_path,
            )
            if skill_id in seen_skill_ids:
                _logger.warning(
                    '[Discover] Duplicate skill_id from different paths, keeping first '
                    'occurrence: skill_id=%s skipped_path=%s',
                    skill_id, relative_path,
                )
                continue
            seen_skill_ids.add(skill_id)
            metadata_content = await asyncio.to_thread(
                self.git_ops.load_skill_frontmatter_from_git_ref,
                repo_root, 'HEAD', relative_path,
            )
            if metadata_content is None:
                continue
            if is_low_value_skill_content(metadata_content[1]):
                _logger.debug(
                    '[Discover] Skipped low-value skill content: skill_id=%s ref=HEAD',
                    skill_id,
                )
                continue
            # 元数据门禁：frontmatter 必须同时声明 name 与 description
            if is_incomplete_skill_metadata(metadata_content[0]):
                _logger.warning(
                    '[Discover] Skipped skill without required metadata '
                    '(name/description): skill_id=%s ref=HEAD',
                    skill_id,
                )
                continue
            input_elapsed = time.perf_counter() - scan_started_at
            tree_hash = tree_hashes.get(relative_path)
            skill_author = (skill_authors or {}).get(relative_path)
            skill_platform = (skill_platforms or {}).get(relative_path)
            skill = await self._build_skill_record(
                repo=repo,
                repo_root=repo_root,
                relative_path=relative_path,
                metadata_content=metadata_content,
                ref=None,
                version='latest',
                commit_id=repository_commit_id,
                tree_hash=tree_hash,
                skill_id=skill_id,
                repository_latest_tags=repository_latest_tags,
                category_cache=category_cache,
                version_source='branch_head',
                author=author,
                scan_started_at=scan_started_at,
                input_elapsed=input_elapsed,
                existing_record=existing_skills.get(skill_id),
                security_cache=security_cache,
                skill_author=skill_author,
                skill_platform=skill_platform,
            )
            _logger.info(
                '[Discover] Discovered skill(latest version:%s): skill_id=%s',
                skill.version or '-', skill.skill_id,
            )
            discovered.append(skill)
            # SKILL.md frontmatter 声明了 version（顶层或 metadata 嵌套）→
            # skill 自管版本：以声明的版本号生成一条 skill_versions 行
            # （version_source=metadata），该 skill 后续完全跳过 tag 扫描。
            declared_version = extract_declared_version(metadata_content[0])
            if declared_version:
                version_record = await self._build_skill_record(
                    repo=repo,
                    repo_root=repo_root,
                    relative_path=relative_path,
                    metadata_content=metadata_content,
                    ref=None,
                    version=declared_version,
                    commit_id=repository_commit_id,
                    tree_hash=tree_hash,
                    skill_id=skill_id,
                    repository_latest_tags=repository_latest_tags,
                    category_cache=category_cache,
                    version_source='metadata',
                    author=author,
                    scan_started_at=scan_started_at,
                    input_elapsed=input_elapsed,
                    return_skill_model=False,
                    existing_record=existing_versions.get(
                        (skill_id, declared_version),
                    ),
                    security_cache=security_cache,
                    skill_author=skill_author,
                    skill_platform=skill_platform,
                )
                _logger.info(
                    '[Discover] Discovered skill(metadata version:%s): skill_id=%s',
                    version_record.version or '-', version_record.skill_id,
                )
                metadata_version_rows.append(version_record)
        return discovered, metadata_version_rows

    async def _scan_tagged_skills(
        self,
        repo: SkillRepoModel,
        repo_root: Path,
        version_snapshots: list[dict[str, str]],
        repository_commit_id: str | None,
        repository_latest_tags: list[str],
        author: str | None,
        existing_versions: dict[tuple[str, str | None], SkillVersion],
        category_cache: dict[str, str | None],
        security_cache: dict[str, SecurityResolution] | None = None,
        skill_path_filter: set[str] | None = None,
        skip_skill_ids: set[str] | None = None,
        skill_authors: dict[str, str] | None = None,
        skill_platforms: dict[str, str] | None = None,
    ) -> list[SkillVersion]:
        discovered: list[SkillVersion] = []
        seen_versions: set[tuple[str, str]] = set()
        seen_commits: set[tuple[str, str]] = set()
        # 内容去重：同一 skill 的多个 tag 指向相同目录内容（tree_hash 一致）时，
        # 只保留最新 tag 的版本行，后续旧 tag 不再作为独立版本写入。
        # version_snapshots 按创建时间降序迭代，首个遇到的即最新 tag。
        seen_tree_hashes: set[tuple[str, str]] = set()

        for snapshot in version_snapshots:
            ref = snapshot['ref']
            version = snapshot.get('version')
            commit_id = snapshot.get('commit_id') or repository_commit_id
            version_source_val = as_optional_str(snapshot.get('version_source'))
            path_listing_started_at = time.perf_counter()
            # Discover paths per ref: a tag may contain skills that were later
            # renamed, removed, or that never existed at HEAD, so HEAD's path
            # set cannot be reused as the universe of tag paths.
            current_tag_skill_paths = await asyncio.to_thread(
                self.git_ops.list_skill_paths_for_ref,
                repo_root, ref,
            )
            # URL 方式贡献（skill_path_filter 非空）的 tag 版本同样只收录
            # 登记的 SKILL.md，防止部分扫描把未贡献的 skill 扫进来
            if skill_path_filter is not None:
                current_tag_skill_paths = [
                    path for path in current_tag_skill_paths
                    if path in skill_path_filter
                ]
            tree_hashes = await asyncio.to_thread(
                self.git_ops.get_skill_tree_hashes,
                repo_root, current_tag_skill_paths, ref=ref,
            )
            _logger.debug(
                '[Discover] Tag skill path listing timing: repo=%s ref=%s current_paths=%d '
                'elapsed=%.3fs',
                getattr(repo, 'repo_name', None) or repo.url,
                ref,
                len(current_tag_skill_paths),
                time.perf_counter() - path_listing_started_at,
            )
            for relative_path in current_tag_skill_paths:
                scan_started_at = time.perf_counter()
                metadata_content = await asyncio.to_thread(
                    self.git_ops.load_skill_frontmatter_from_git_ref,
                    repo_root, ref, relative_path,
                )
                if metadata_content is None:
                    continue
                if is_low_value_skill_content(metadata_content[1]):
                    _logger.debug(
                        '[Discover] Skipped low-value skill content: path=%s ref=%s',
                        relative_path, ref,
                    )
                    continue
                # 元数据门禁：frontmatter 必须同时声明 name 与 description
                if is_incomplete_skill_metadata(metadata_content[0]):
                    _logger.warning(
                        '[Discover] Skipped skill without required metadata '
                        '(name/description): path=%s ref=%s',
                        relative_path, ref,
                    )
                    continue
                input_elapsed = time.perf_counter() - scan_started_at
                tree_hash = tree_hashes.get(relative_path)
                skill_author = (skill_authors or {}).get(relative_path)
                skill_platform = (skill_platforms or {}).get(relative_path)
                skill_id = build_skill_id(
                    repo.source, extract_owner_repo(repo.url), relative_path,
                )
                # HEAD 声明了 frontmatter version 的 skill 自管版本，
                # 完全跳过 tag 扫描（版本行由 latest 扫描生成）。
                if skip_skill_ids and skill_id in skip_skill_ids:
                    _logger.debug(
                        '[Discover] Skipped tag scan for metadata-versioned skill: '
                        'skill_id=%s ref=%s',
                        skill_id,
                        ref,
                    )
                    continue
                # 版本号以元数据为准：SKILL.md frontmatter 声明了 version
                # （顶层或 metadata 嵌套）时优先使用，仅当元数据缺失时
                # 才回退到 tag 名作为版本号。
                metadata_version = extract_declared_version(metadata_content[0])
                effective_version = metadata_version or version
                effective_version_source = (
                    'metadata' if metadata_version else version_source_val
                )
                # 内容去重前置检查：同 tree_hash 的旧 tag 直接跳过，
                # 省去昂贵的记录组装（含安全审计解析）。
                tree_hash_key = (skill_id, tree_hash) if tree_hash else None
                if tree_hash_key is not None and tree_hash_key in seen_tree_hashes:
                    _logger.debug(
                        '[Discover] Skipped duplicate skill tree_hash: skill_id=%s '
                        'version=%s tree_hash=%s',
                        skill_id,
                        version or '-',
                        tree_hash or '-',
                    )
                    continue
                # Pre-dedup on commit before building the record: skips the
                # expensive record assembly (incl. security resolution) for
                # tags that point at an already-seen commit of the same skill.
                pre_commit_key = (skill_id, commit_id) if commit_id else None
                if pre_commit_key is not None and pre_commit_key in seen_commits:
                    _logger.debug(
                        '[Discover] Skipped duplicate skill commit (pre-check): skill_id=%s '
                        'version=%s commit_id=%s',
                        skill_id,
                        version or '-',
                        commit_id or '-',
                    )
                    continue
                skill = await self._build_skill_record(
                    repo=repo,
                    repo_root=repo_root,
                    relative_path=relative_path,
                    metadata_content=metadata_content,
                    ref=ref,
                    version=effective_version,
                    commit_id=commit_id,
                    tree_hash=tree_hash,
                    skill_id=skill_id,
                    repository_latest_tags=repository_latest_tags,
                    category_cache=category_cache,
                    version_source=effective_version_source,
                    author=author,
                    scan_started_at=scan_started_at,
                    input_elapsed=input_elapsed,
                    return_skill_model=False,
                    existing_record=existing_versions.get((skill_id, effective_version)),
                    security_cache=security_cache,
                    skill_author=skill_author,
                    skill_platform=skill_platform,
                )
                version_key = (skill.skill_id, skill.version) if skill.version else None
                if version_key is not None and version_key in seen_versions:
                    _logger.debug(
                        '[Discover] Skipped duplicate skill version: skill_id=%s version=%s commit_id=%s',
                        skill.skill_id,
                        skill.version or '-',
                        skill.commit_id or '-',
                    )
                    continue
                if version_key is not None:
                    seen_versions.add(version_key)
                # Commit dedup happens in the pre-check above; this write feeds it.
                if commit_id:
                    seen_commits.add((skill_id, commit_id))
                if tree_hash_key is not None:
                    seen_tree_hashes.add(tree_hash_key)
                _logger.info(
                    '[Discover] Discovered skill(version:%s): skill_id=%s',
                    skill.version or '-', skill.skill_id,
                )
                discovered.append(skill)

        return discovered

    async def _build_skill_record(
        self,
        repo: SkillRepoModel,
        repo_root: Path,
        relative_path: str,
        metadata_content: tuple[dict[str, object], str],
        ref: str | None,
        version: str | None,
        commit_id: str | None,
        tree_hash: str | None,
        skill_id: str,
        repository_latest_tags: list[str],
        category_cache: dict[str, str | None],
        version_source: str | None,
        author: str | None,
        scan_started_at: float,
        input_elapsed: float,
        *,
        return_skill_model: bool = True,
        existing_record: Skill | SkillVersion | None = None,
        security_cache: dict[str, SecurityResolution] | None = None,
        skill_author: str | None = None,
        skill_platform: str | None = None,
    ) -> Skill | SkillVersion:
        prepare_started_at = time.perf_counter()
        skill_file = repo_root / relative_path
        metadata, content = metadata_content
        merged_metadata = dict(metadata)
        if repository_latest_tags and 'repository_latest_tags' not in merged_metadata:
            merged_metadata['repository_latest_tags'] = repository_latest_tags
        if version_source and 'version_source' not in merged_metadata:
            merged_metadata['version_source'] = version_source
        source_url = build_skill_md_url(repo, relative_path, ref=ref)
        if source_url is None:
            raise ValueError(
                f'Failed to build source_url for repository {repo.id}: {relative_path}'
            )
        prepare_elapsed = time.perf_counter() - prepare_started_at

        context_started_at = time.perf_counter()
        host, source_author = derive_skill_source(repo.url)
        author = skill_author or author or source_author
        prepare_elapsed += time.perf_counter() - context_started_at

        category_started_at = time.perf_counter()
        category = await self._classify_skill_category(
            skill_file=skill_file,
            metadata=merged_metadata,
            content=content or '',
            source_url=source_url,
            skill_id=skill_id,
            category_cache=category_cache,
        )
        category_elapsed = time.perf_counter() - category_started_at

        security_started_at = time.perf_counter()
        security = await self._resolve_security_result(
            repo=repo,
            relative_path=relative_path,
            skill_id=skill_id,
            version=version,
            commit_id=commit_id,
            tree_hash=tree_hash,
            existing_record=existing_record,
            security_cache=security_cache,
        )
        if security.audit_details:
            merged_metadata['security_audit'] = security.audit_details
        security_elapsed = time.perf_counter() - security_started_at

        assemble_started_at = time.perf_counter()
        common = {
            'skill_repo_id': repo.id,
            'skill_id': skill_id,
            'name': derive_repository_skill_name(skill_file, metadata),
            'description': as_optional_str(metadata.get('description')),
            'version': version or as_optional_str(metadata.get('version')),
            'commit_id': commit_id,
            'tree_hash': tree_hash,
            'author': author,
            'source': host,
            'source_url': source_url,
            'repo_url': repo.url,
            'category': category,
            'tags': extract_declared_tags(metadata),
            'platform': skill_platform
            or as_optional_str(getattr(repo, 'platform', None))
            or as_optional_str(metadata.get('platform')),
            'extra_metadata': merged_metadata,
            'content': content or None,
            'risk_score': security.risk_score,
            'created_at': datetime.now(timezone.utc),
            'updated_at': datetime.now(timezone.utc),
        }
        model_class = Skill if return_skill_model else SkillVersion
        record = model_class(**common)
        setattr(record, '_security_audit_triggered', security.audit_triggered)
        setattr(record, '_security_audit_attempted', security.audit_attempted)
        assemble_elapsed = time.perf_counter() - assemble_started_at
        total_elapsed = time.perf_counter() - scan_started_at
        accounted_elapsed = (
            input_elapsed
            + prepare_elapsed
            + category_elapsed
            + security_elapsed
            + assemble_elapsed
        )
        _logger.debug(
            '[Discover] Skill timing: skill_id=%s version=%s input=%.3fs prepare=%.3fs '
            'category=%.3fs security=%.3fs assemble=%.3fs '
            'other=%.3fs total=%.3fs',
            skill_id,
            version or 'latest',
            input_elapsed,
            prepare_elapsed,
            category_elapsed,
            security_elapsed,
            assemble_elapsed,
            max(0.0, total_elapsed - accounted_elapsed),
            total_elapsed,
        )
        return record

    async def _resolve_security_result(
        self,
        *,
        repo: SkillRepoModel,
        relative_path: str,
        skill_id: str,
        version: str | None,
        commit_id: str | None,
        tree_hash: str | None,
        existing_record: Skill | SkillVersion | None,
        security_cache: dict[str, SecurityResolution] | None = None,
    ) -> SecurityResolution:
        existing_metadata = (
            dict(existing_record.extra_metadata or {})
            if existing_record is not None
            else {}
        )
        if (
            existing_record is not None
            and tree_hash is not None
            and tree_hash == existing_record.tree_hash
            and existing_record.risk_score is not None
        ):
            audit_details = existing_metadata.get('security_audit')
            _logger.debug(
                '[Discover] Reused security result: skill_id=%s version=%s tree_hash=%s score=%s',
                skill_id,
                version or '-',
                tree_hash,
                existing_record.risk_score,
            )
            resolution = SecurityResolution(
                risk_score=existing_record.risk_score,
                audit_details=dict(audit_details) if audit_details else None,
                audit_triggered=False,
            )
            if security_cache is not None and tree_hash is not None:
                security_cache[tree_hash] = resolution
            self._security_cache_hits += 1
            return resolution

        if security_cache is not None and tree_hash is not None:
            cached = security_cache.get(tree_hash)
            if cached is not None:
                _logger.debug(
                    '[Discover] Reused cache submitted by security: skill_id=%s', skill_id,
                )
                self._security_cache_hits += 1
                if cached.audit_triggered:
                    self._security_audit_pending += 1
                return SecurityResolution(
                    risk_score=cached.risk_score,
                    audit_details=dict(cached.audit_details) if cached.audit_details else None,
                    audit_triggered=cached.audit_triggered,
                    audit_attempted=cached.audit_attempted,
                )

        report = await self._audit_skill_security(
            repo=repo,
            relative_path=relative_path,
            commit_id=commit_id,
            skill_id=skill_id,
        )
        risk_score, audit_details = self._extract_audit_artifacts(report)
        resolution = SecurityResolution(
            risk_score=risk_score,
            audit_details=audit_details,
            audit_triggered=bool(
                audit_details
                and audit_details.get('skillspector_async')
                and audit_details.get('skillspector_build_number') is not None
            ),
            audit_attempted=bool(
                audit_details
                and audit_details.get('skillspector_async')
            ),
        )
        self._security_audit_submitted += 1
        if resolution.audit_triggered:
            self._security_audit_pending += 1
        # Only cache usable outcomes: a final score (sync result / DB reuse)
        # or an async audit awaiting retrieval. Failed audits stay out so
        # identical-content versions retry instead of inheriting the failure.
        if (
            security_cache is not None
            and tree_hash is not None
            and (resolution.risk_score is not None or resolution.audit_triggered)
        ):
            security_cache[tree_hash] = resolution
        return resolution

    async def _classify_skill_category(
        self,
        *,
        skill_file: Path,
        metadata: dict[str, object],
        content: str,
        source_url: str,
        skill_id: str,
        category_cache: dict[str, str | None],
    ) -> str | None:
        if skill_id in category_cache:
            return category_cache[skill_id]

        # frontmatter 声明了 category 且值是合法的标准分类 → 直接使用，
        # 跳过分类器。值不合法时回退到原逻辑（分类器或原样透传）。
        declared_category = as_optional_str(metadata.get('category'))
        if declared_category and declared_category in CANONICAL_CATEGORIES:
            category_cache[skill_id] = declared_category
            return declared_category

        if self.category_classifier is None:
            category_cache[skill_id] = declared_category
            return declared_category
        category = await asyncio.to_thread(
            self.category_classifier.classify,
            skill_file=skill_file,
            metadata=metadata,
            content=content,
            source_url=source_url,
        )
        category_cache[skill_id] = category
        return category

    async def _audit_skill_security(
        self,
        *,
        repo: SkillRepoModel,
        relative_path: str,
        commit_id: str | None,
        skill_id: str,
    ) -> SecurityReport | None:
        """Trigger a security audit for one skill record.

        The crawler stays DB-agnostic; ``SkillManager`` persists the
        ``SecurityAudit`` row after the skill is stored.
        """
        return await trigger_existing_skill_audit(
            git_ops=self.git_ops,
            security_detector=self.security_detector,
            security_async_mode=self.security_async_mode,
            repo=repo,
            relative_path=relative_path,
            commit_id=commit_id,
            skill_id=skill_id,
        )

    @staticmethod
    def _extract_audit_artifacts(
        report: SecurityReport | None,
    ) -> tuple[int | None, dict[str, Any] | None]:
        """Translate a ``SecurityReport`` into ``risk_score`` + storable details."""
        if report is None:
            return None, None

        score = report.details.get('skillspector_score')
        if score is None:
            level = (report.risk_level or 'unknown').lower()
            score_map = {'critical': 90, 'high': 65, 'medium': 35, 'low': 10}
            score = score_map.get(level) if level != 'unknown' else None

        details = dict(report.details) if report.details else None
        if details:
            details.setdefault('risk_level', report.risk_level)
            details.setdefault(
                'risk_signals',
                [s.__dict__ for s in report.risk_signals],
            )
        return score, details
