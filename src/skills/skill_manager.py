"""SkillManager: orchestrates skill repo CRUD and discovery."""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from sqlalchemy import update

from src.skills.config import CATALOG_PLATFORMS, load_contributor_profiles
from src.ai.category_classifier import (
    CategoryClassificationError,
    DeepSeekCategoryClassifier,
)
from src.utils.git_operations import GitOperations
from src.skills.openeuler_sig import build_openeuler_repo_sig_mapping
from src.skills.skill_parser import (
    as_optional_str,
    derive_skill_source,
    normalize_clone_url_for_git,
    normalize_git_clone_url,
)
from src.skills.skill_scanner import SkillScanner
from src.skills.skill_id import build_skill_id, extract_owner_repo
from src.core.config import get_settings
from src.models.orm import Skill, SkillVersion
from src.models.repository import (
    ContributorRepository,
    SecurityAuditRepository,
    SkillRepoRepository,
    SkillRepository,
)
from src.security.detector import SecurityDetector

if TYPE_CHECKING:
    from src.models.orm import SkillRepoModel

_logger = logging.getLogger(__name__)
settings = get_settings()


class SkillRepositoryRequest(BaseModel):
    branch: str | None = None
    url: str | None = None
    platform: str | None = None
    sig_name: str | None = None
    # URL 方式贡献的单 Skill 收窄扫描（skills[].skill_url）：非空时只收录
    # 这些 SKILL.md 相对路径；None 表示整仓扫描
    skill_paths: list[str] | None = None
    # 原文件方式贡献（local_skills）：相对路径 → author / platform 两个平行映射，
    # 用于同一仓库（openEuler-skills catalog）内多个作者各自的 Skill 归属覆盖。
    skill_authors: dict[str, str] | None = None
    skill_platforms: dict[str, str] | None = None


class SkillDiscoverStatus:
    INIT = 'init'
    DISCOVERING = 'discovering'
    DONE = 'done'
    FAILED = 'failed'


@dataclass(slots=True)
class SkillManager:
    skill_repository: SkillRepository
    skill_repo_repository: SkillRepoRepository
    contributor_repository: ContributorRepository | None = None
    workspace_base: Path | None = None
    catalog_path: Path | None = None
    _git_ops: GitOperations = field(init=False, repr=False)
    _scanner: SkillScanner = field(init=False, repr=False)
    _openeuler_sig_by_repo_name: dict[str, str] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _contributor_profile_by_key: dict[tuple[str, str], dict[str, Any]] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.workspace_base is None:
            self.workspace_base = Path(settings.storage.local_path).expanduser().resolve()

        category_classifier: DeepSeekCategoryClassifier | None = None
        security_detector: SecurityDetector | None = None
        try:
            category_classifier = DeepSeekCategoryClassifier(self.catalog_path)
        except Exception as exc:
            _logger.warning('[Discover] Failed to initialize category classifier: %s', exc)

        try:
            detector = SecurityDetector()
            if detector.has_skillspector:
                security_detector = detector
            else:
                _logger.info('[Discover] SecurityDetector: skillspector not configured')
        except Exception as exc:
            _logger.warning('[Discover] SecurityDetector: Failed to initialize security detector: %s', exc)

        self._git_ops = GitOperations()
        self._scanner = SkillScanner(
            git_ops=self._git_ops,
            skill_repository=self.skill_repository,
            category_classifier=category_classifier,
            security_detector=security_detector,
            security_async_mode=True,
        )

    # ── Public API ─────────────────────────────────────────────────

    async def list_skill_repositories(self) -> list[SkillRepoModel]:
        return await self.skill_repo_repository.list_skill_repositories()

    async def rollback(self) -> None:
        """Restore the shared database session after a failed repository scan."""
        await self.skill_repository.session.rollback()

    async def create_skill_repository(
        self, normalized_request: SkillRepositoryRequest, repo_name: str,
    ) -> SkillRepoModel:
        source, _ = derive_skill_source(normalized_request.url)
        return await self.skill_repo_repository.create_skill_repository(
            repo_name=repo_name,
            source=source,
            branch=normalized_request.branch,
            url=normalized_request.url,
            local_path=None,
            skill_discover_status=SkillDiscoverStatus.INIT,
            platform=normalized_request.platform,
        )

    def _cleanup_local_clone(self, local_path: Any) -> None:
        """删除仓库的本地 clone 目录；失败仅告警。"""
        if not local_path:
            return
        path = Path(local_path)
        if path.is_dir():
            try:
                shutil.rmtree(path)
            except Exception as exc:
                _logger.warning(f'[Discover] Failed to clean up directory {path}: {exc}')

    async def delete_skill_repository(self, repository_id: str) -> None:
        stored = await self.get_repository_by_id(repository_id)
        # DB 删除成功后再清本地 clone 目录，失败时保持本地与 DB 一致
        await self.skill_repo_repository.delete_skill_repository(stored.id)
        self._cleanup_local_clone(stored.local_path)
        # 删除后自愈统计值（contributors.skill_count / skill_repos.skill_num），
        # 内部容错，失败仅告警不影响删除结果
        await self._reconcile_skill_statistics()

    async def get_repository_by_id(self, repository_id: str) -> SkillRepoModel:
        repository = await self.skill_repo_repository.get_skill_repository_by_id(repository_id)
        if repository is None:
            raise KeyError(f'Skill repo {repository_id} not found')
        return repository

    # ── Discover from configured repo list ────────────────────────

    def _list_skill_paths(self, clone_dir: Path) -> list[str]:
        """List the scannable SKILL.md paths at HEAD for a cloned repo."""
        return self._git_ops.list_skill_paths_for_ref(clone_dir, 'HEAD')

    async def discover_configured_skill_repository(
        self,
        request: SkillRepositoryRequest,
    ) -> SkillRepoModel | None:
        """Clone a repo and persist it only when a scannable skill is present.

        Returns ``None`` when no scannable ``SKILL.md`` exists and the repository
        has no persisted record. Existing records are deleted when their clone no
        longer contains a scannable skill.

        Clone and discovery errors propagate to the caller. The clone directory is
        retained for troubleshooting and reuse by a later scan.
        """
        normalized = self._normalize_repository_request(request)
        repo_name = self._derive_repo_name(normalized)
        clone_dir = self._local_repository_path(repo_name)
        # git 同步（clone/fetch）放到线程池执行，网络挂起时不再阻塞事件循环
        await asyncio.to_thread(
            self._sync_git_repository,
            clone_dir=clone_dir,
            clone_url=normalize_clone_url_for_git(normalized.url),
            branch=normalized.branch,
            repo_url=normalized.url,
        )

        skill_paths = await asyncio.to_thread(self._list_skill_paths, clone_dir)
        # URL 方式贡献（skill_paths 非空）只收录登记的 SKILL.md，其余不入库
        skill_path_filter: set[str] | None = None
        if normalized.skill_paths:
            skill_path_filter = set(normalized.skill_paths)
            skill_paths = [path for path in skill_paths if path in skill_path_filter]
        if not skill_paths:
            _logger.info(
                '[Discover] no scannable SKILL.md found in %s; retaining clone at %s',
                repo_name,
                clone_dir,
            )
            repository = await self.skill_repo_repository.get_skill_repository_by_repo_name(repo_name)
            if repository is not None:
                setattr(repository, "_removed_existing", True)
                # 由于 skills / skill_versions 表对 skill_repos 有 ondelete="CASCADE" 外键（ orm.py ），
                # 删 repository 时其下所有 skill 记录会 级联删除
                await self.skill_repo_repository.delete_skill_repository(repository.id)
                if repository.author and self.contributor_repository is not None:
                    # 仓库删除后重新聚合该贡献者的 skill_count（同 author 的
                    # 其他仓库不受影响）；repo_url 传 None 保留已有值
                    await self.contributor_repository.upsert(
                        source=repository.source,
                        author=repository.author,
                        platform=repository.platform,
                        repo_url=None,
                        skill_count=await self.contributor_repository.count_skills(
                            repository.source, repository.author
                        ),
                    )
                return repository
            return None

        repository, created_new = await self._get_or_create_skill_repository(
            normalized,
            repo_name,
        )
        if not created_new:
            # 此前以直接采集方式（platform=''）收录的仓库，若后续在锚点目录中
            # 登记（platform='community'），即使 git 提交未变化，也必须迁移其
            # 列表来源字段（platform/source）。
            repository = await self._sync_catalog_source(repository, normalized)
            if await asyncio.to_thread(
                self._is_commit_unchanged, repository, clone_dir,
            ):
                setattr(repository, "_unchanged", True)
                return repository

        # Cache values needed by the error path before any commit/rollback can
        # expire the ORM instance, otherwise accessing them after rollback triggers
        # async lazy loading outside greenlet_spawn.
        repository_id_value = repository.id
        repository_name_value = repository.repo_name
        repository_skill_num = repository.skill_num

        await self._set_discovery_status(repository, SkillDiscoverStatus.DISCOVERING)
        try:
            refreshed = await self._discover_and_store_skills(
                repository,
                clone_dir=clone_dir,
                repo_name=repo_name,
                skill_paths=skill_paths,
                skill_path_filter=skill_path_filter,
                author=normalized.sig_name,
                skill_authors=normalized.skill_authors,
                skill_platforms=normalized.skill_platforms,
            )
        except Exception as exc:
            error_summary = self._git_ops.summarize_exception(exc)
            _logger.warning(
                '[Discover] Failed to discover skills from skill repo %s (%s): %s',
                repository_id_value, repository_name_value, error_summary,
            )
            await self.rollback()
            await self.skill_repo_repository.update_skill_repository(
                repository_id_value,
                skill_discover_status=SkillDiscoverStatus.FAILED,
                skill_num=repository_skill_num,
            )
            if isinstance(exc, CategoryClassificationError):
                raise
            raise ValueError(
                f'Failed to discover skills from skill repo {repository_id_value}: {error_summary}'
            ) from exc
        setattr(refreshed, "_created_new", created_new)
        return refreshed

    # ── Internal: discovery orchestration ──────────────────────────

    async def _discover_and_store_skills(
        self,
        repo: SkillRepoModel,
        *,
        clone_dir: Path,
        repo_name: str,
        skill_paths: list[str] | None = None,
        skill_path_filter: set[str] | None = None,
        author: str | None = None,
        skill_authors: dict[str, str] | None = None,
        skill_platforms: dict[str, str] | None = None,
    ) -> SkillRepoModel:
        author = author or self._resolve_skill_author(repo.platform, repo_name)
        latest_skills, tagged_skills, repository_commit_id = await self._discover_skills(
            repo,
            clone_dir=clone_dir,
            author=author,
            skill_paths=skill_paths,
            skill_path_filter=skill_path_filter,
            skill_authors=skill_authors,
            skill_platforms=skill_platforms,
        )
        # enterprise/personal 仓库 author 未显式传入（community 走 sig_name）时，
        # 用扫描结果里 URL 推导的 author 兜底，保证 skill_repos.author 与
        # contributors 表对全部 platform 都有值
        if not author and latest_skills:
            author = next(
                (skill.author for skill in latest_skills if skill.author), None
            )
        _logger.info(
            '[Discover] scan completed for %s: latest_skills=%d, tagged_skills=%d, '
            'security_cache_hits=%d, security_audits_submitted=%d, security_audits_pending=%d',
            repo.repo_name, len(latest_skills), len(tagged_skills),
            self._scanner._security_cache_hits,
            self._scanner._security_audit_submitted,
            self._scanner._security_audit_pending,
        )
        unique_skill_count = self._count_unique_skills(latest_skills)
        await self.skill_repository.store_skills_and_versions(
            repo.id,
            latest_skills,
            tagged_skills,
            commit=False,
        )
        await self._store_to_security_audits(
            latest_skills,
            tagged_skills,
        )
        # 逐 skill 聚合 contributor：platform / author / source 取自每个 skill，
        # 而非仓库级 repo.platform / 单一 author。local_skills 自扫描（仓库级
        # platform=None）下一个仓库会混合 community/enterprise/personal 多个
        # 作者，逐 skill 才能让每个贡献者落到正确的 platform（否则个人贡献者
        # platform 落空，贡献者广场按 platform 过滤时会漏掉）。
        # profile 字段取自 openEuler-skills catalog 各 platform 的 skill.yaml。
        if self.contributor_repository is not None and latest_skills:
            seen_contributors: set[tuple[str, str]] = set()
            for skill in latest_skills:
                skill_source = as_optional_str(skill.source)
                skill_author = as_optional_str(skill.author)
                skill_platform = as_optional_str(skill.platform)
                if not skill_source or not skill_author:
                    continue
                dedup_key = (skill_source, skill_author)
                if dedup_key in seen_contributors:
                    continue
                seen_contributors.add(dedup_key)
                profile = self._contributor_profile(skill_platform, skill_author)
                # skill_count 从 skills 表实时聚合该 author 名下全部可见 skill
                # （含同 author 的其他仓库），避免多仓库 upsert 互相覆盖单仓库扫描值
                contributor_skill_count = await self.contributor_repository.count_skills(
                    skill_source, skill_author
                )
                await self.contributor_repository.upsert(
                    source=skill_source,
                    author=skill_author,
                    platform=skill_platform,
                    repo_url=repo.url,
                    skill_count=contributor_skill_count,
                    name=profile.get('name'),
                    description=profile.get('description'),
                    git_profile=profile.get('git_profile'),
                    website=profile.get('website'),
                    commit=False,
                )
        repository = await self.skill_repo_repository.update_skill_repository(
            repo.id,
            repository_commit_id=repository_commit_id,
            skill_discover_status=SkillDiscoverStatus.DONE,
            skill_num=unique_skill_count,
            author=author,
        )
        await self._reconcile_skill_statistics()
        return repository

    async def _reconcile_skill_statistics(self) -> None:
        """扫描落库后按 skills 表实际数据自愈统计值。

        修正三类漂移：skill 被删除后 contributors.skill_count /
        skill_repos.skill_num 的残留、多作者仓库只刷新单一 author 的
        contributors 行、以及手工直改 skills 表造成的偏差。统计口径
        与扫描写入一致（contributors 走 risk_score 非空的可见口径，
        skill_repos 走仓库下 skill 行数），因此不会与扫描互相翻转。
        失败仅告警，不影响已提交的扫描结果。
        """
        try:
            corrected_contributors = 0
            if self.contributor_repository is not None:
                corrected_contributors = (
                    await self.contributor_repository.reconcile_skill_counts()
                )
            corrected_repos = await self.skill_repo_repository.reconcile_skill_nums()
            if corrected_contributors or corrected_repos:
                _logger.info(
                    '[Discover] statistics reconciled after scan: '
                    'contributors=%d, skill_repos=%d',
                    corrected_contributors,
                    corrected_repos,
                )
        except Exception as exc:
            _logger.warning('[Discover] Failed to reconcile skill statistics after scan: %s', exc)

    async def delete_removed_catalog_repos(
        self, requests: list[SkillRepositoryRequest]
    ) -> int:
        """同步删除 catalog 中已消失的仓库及其级联数据，返回删除的仓库数。

        按 (platform, author, repo_name) 求交集：数据库中在 catalog 期望集合
        外的组合即已删除。author 口径与扫描写入 skill_repos.author 一致。

        skill / skill_versions 走外键 CASCADE；security_audits / download_history
        软关联由 ``delete_skill_repositories`` 清理；删除后重算 contributors
        .skill_count 并清理名下已无 skill 的孤儿作者。
        """
        expected_keys: set[tuple[str, str | None, str]] = set()
        for request in requests:
            if request.platform not in CATALOG_PLATFORMS:
                continue
            normalized = self._normalize_repository_request(request)
            expected_keys.add(
                (
                    normalized.platform,
                    self._derive_catalog_author(normalized),
                    self._derive_repo_name(normalized),
                )
            )

        repos = await self.skill_repo_repository.list_skill_repositories()
        removed = [
            repo for repo in repos
            if repo.platform in CATALOG_PLATFORMS
            and (repo.platform, repo.author, repo.repo_name) not in expected_keys
        ]
        if not removed:
            return 0

        removed_ids = [repo.id for repo in removed]
        _logger.info(
            '[Discover] syncing catalog removals: deleting %d repo(s) %s',
            len(removed_ids),
            [repo.repo_name for repo in removed],
        )
        await self.skill_repo_repository.delete_skill_repositories(removed_ids)

        # 删除后自愈统计值并清理孤儿作者；内部容错，失败仅告警不影响删除结果
        await self._reconcile_skill_statistics()
        deleted_contributors = 0
        if self.contributor_repository is not None:
            deleted_contributors = (
                await self.contributor_repository.delete_contributors_without_skills()
            )
        _logger.info(
            '[Discover] catalog removal sync done: repos_deleted=%d, '
            'empty_contributors_deleted=%d',
            len(removed_ids),
            deleted_contributors,
        )
        return len(removed_ids)

    async def delete_removed_catalog_skills(
        self, requests: list[SkillRepositoryRequest]
    ) -> int:
        """同步删除 catalog 中已撤销登记的单 Skill（URL / local_skills 条目移除）。

        对每条「部分扫描」请求（``skill_paths`` 非空：``skills[].skill_url``
        单 Skill 与 ``local_skills`` 原文件方式），按同一口径推导该仓库期望
        存在的 skill_id 集合，删除仓库下不属于集合的 skill（含 version 与
        security_audits / download_history 软关联）。整仓移除由
        ``delete_removed_catalog_repos`` 负责；整仓请求（skill_paths=None）
        在这里跳过——它是全量扫描，store 的 diff 已覆盖。
        返回删除的 skill 行数。
        """
        deleted = 0
        for request in requests:
            if not request.skill_paths:
                continue
            normalized = self._normalize_repository_request(request)
            repo_name = self._derive_repo_name(normalized)
            repository = await self.skill_repo_repository.get_skill_repository_by_repo_name(repo_name)
            if repository is None:
                continue
            source, _ = derive_skill_source(normalized.url)
            owner_repo = extract_owner_repo(normalized.url)
            expected_skill_ids = {
                build_skill_id(source, owner_repo, path)
                for path in normalized.skill_paths
            }
            removed = await self.skill_repository.delete_skills_not_in_skill_ids(
                repository.id, expected_skill_ids
            )
            if removed:
                deleted += removed
                _logger.info(
                    '[Discover] removed %d revoked skill(s) from %s',
                    removed,
                    repo_name,
                )
        if deleted:
            await self._reconcile_skill_statistics()
        return deleted

    def _contributor_profile(self, platform: str | None, author: str) -> dict[str, Any]:
        """Lookup catalog profile fields (name/description/git_profile) for a contributor.

        Lazily loads all profiles from the openEuler-skills catalog
        (community/enterprise/personal skill.yaml) and indexes them by
        (platform, author). Returns an empty dict when the catalog is
        unavailable or the contributor is not registered.
        """
        if self.catalog_path is None:
            return {}
        if self._contributor_profile_by_key is None:
            try:
                profiles = load_contributor_profiles(self.catalog_path)
                self._contributor_profile_by_key = {
                    (p['platform'], p['author']): p for p in profiles
                }
                _logger.info(
                    '[Discover] Loaded contributor profiles from catalog: %d entries',
                    len(self._contributor_profile_by_key),
                )
            except Exception as exc:
                _logger.warning(
                    '[Discover] Failed to load contributor profiles from catalog %s: %s',
                    self.catalog_path,
                    exc,
                )
                self._contributor_profile_by_key = {}
        return self._contributor_profile_by_key.get((platform or '', author), {})

    async def _discover_skills(
        self,
        repo: SkillRepoModel,
        *,
        clone_dir: Path,
        author: str | None,
        skill_paths: list[str] | None = None,
        skill_path_filter: set[str] | None = None,
        skill_authors: dict[str, str] | None = None,
        skill_platforms: dict[str, str] | None = None,
    ) -> tuple[list[Skill], list[SkillVersion], str | None]:
        clone_url = normalize_clone_url_for_git(repo.url)
        repository_git_metadata = await asyncio.to_thread(
            self._git_ops.collect_repository_git_metadata,
            clone_dir,
            clone_url,
            repo.url,
        )
        _logger.info(
            '[Discover] git metadata collected for %s, latest_tags: %s',
            repo.repo_name,
            repository_git_metadata.get('latest_tags', []),
        )

        detected_branch: str | None = None
        if repo.branch is None:
            detected_branch = await asyncio.to_thread(
                self._git_ops.get_cloned_repo_branch, clone_dir,
            )
        repo = await self.skill_repo_repository.update_skill_repository(
            repo.id,
            branch=detected_branch,
            local_path=str(clone_dir),
            commit=False,
        )
        repository_commit_id = as_optional_str(repository_git_metadata.get('commit_id'))
        latest_skills, tagged_skills = await self._scanner.start_scan(
            repo=repo,
            repo_root=clone_dir,
            repository_git_metadata=repository_git_metadata,
            author=author,
            skill_paths=skill_paths,
            skill_path_filter=skill_path_filter,
            skill_authors=skill_authors,
            skill_platforms=skill_platforms,
        )
        return latest_skills, tagged_skills, repository_commit_id

    async def _store_to_security_audits(
        self,
        latest_skills: list[Skill],
        tagged_skills: list[SkillVersion],
    ) -> None:
        """Create ``SecurityAudit`` records for async-triggered skills.

        触发失败（Jenkins 不可达等）的记录同样落库 risk_level=unknown，
        与 API 审计路径（SecurityService.audit_skill）保持一致，
        保证审计链路故障时审计状态仍可观测。
        """
        records: list[Skill | SkillVersion] = [*latest_skills, *tagged_skills]
        # Records without a database id were never persisted (e.g. dropped as
        # skill_id duplicates); writing their audit would violate the
        # resource_id NOT NULL constraint and fail the whole transaction.
        pending = [
            record for record in records
            if (
                self._has_new_security_audit(record)
                or self._has_attempted_security_audit(record)
            )
            and record.id is not None
        ]

        if not pending:
            return

        audit_repo = SecurityAuditRepository(self.skill_repository.session)
        entries = [
            (
                record.id,
                {
                    'resource_type': 'skill',
                    'resource_id': record.id,
                    'version': record.version,
                    'commit_id': record.commit_id,
                    'audit_type': 'skillspector',
                    'risk_level': 'unknown',
                    'risk_signals': [],
                    'details': dict(record.extra_metadata['security_audit']),
                },
            )
            for record in pending
        ]
        await audit_repo.upsert_many_by_resource('skill', entries)

        _logger.info('[Discover] SecurityDetector: Upserted %d SecurityAudit records', len(pending))

    @staticmethod
    def _has_new_security_audit(record: Skill | SkillVersion) -> bool:
        """Return whether this scan triggered a new audit for the record."""
        return bool(getattr(record, '_security_audit_triggered', False))

    @staticmethod
    def _has_attempted_security_audit(record: Skill | SkillVersion) -> bool:
        """Return whether an async audit was attempted even if trigger failed."""
        return bool(getattr(record, '_security_audit_attempted', False))

    # ── Internal: helpers ──────────────────────────────────────────

    def _local_repository_path(self, repo_name: str) -> Path:
        return self.workspace_base / 'skill-repositories' / repo_name

    def _sync_git_repository(
        self,
        *,
        clone_dir: Path,
        clone_url: str,
        branch: str | None,
        repo_url: str | None,
    ) -> None:
        if clone_dir.is_dir() and (clone_dir / '.git').exists():
            _logger.info('[Discover] updating skill repo: %s', clone_dir)
            self._git_ops.update_existing_repository(
                clone_dir, clone_url,
                branch=branch, repo_url=repo_url,
            )
            return

        if clone_dir.exists():
            _logger.info('[Discover] removing non-git directory before clone: %s', clone_dir)
            shutil.rmtree(clone_dir)
        _logger.info('[Discover] cloning skill repo: %s', clone_dir)
        clone_dir.mkdir(parents=True, exist_ok=True)
        self._git_ops.clone_repository(
            clone_dir, clone_url,
            branch=branch, repo_url=repo_url,
        )

    async def _get_or_create_skill_repository(
        self,
        normalized_request: SkillRepositoryRequest,
        repo_name: str,
    ) -> tuple[SkillRepoModel, bool]:
        existing_repository = await self.skill_repo_repository.get_skill_repository_by_repo_name(repo_name)
        if existing_repository is not None:
            _logger.info(
                f'[Discover] Skill repo already exists, "{repo_name}": "{existing_repository.id}", skipping creation'
            )
            return existing_repository, False

        repository = await self.create_skill_repository(normalized_request, repo_name)
        return repository, True

    async def _sync_catalog_source(
        self,
        repo: SkillRepoModel,
        normalized_request: SkillRepositoryRequest,
    ) -> SkillRepoModel:
        """将 skill_repos / skills 的列表来源字段刷新为目录视图。

        此前通过直接采集方式（``discover --url``，platform 为空）收录的仓库，
        若后续在锚点目录中登记（如 ``platform='community'``），则下一次采集时
        必须迁移其 ``platform``/``source`` 字段，即使 git 提交未变化也是如此。
        否则技能列表会一直显示旧来源，直到仓库内容发生变化。

        author 与 catalog 的差异不在此处原地迁移，统一交由同步删除阶段
        （``delete_removed_catalog_repos``）处理：数据库有、catalog 无的
        (platform, author, repo) 组合会被删除，之后重新扫描写入正确归属。
        """
        target_source: str | None = None
        try:
            target_source, _ = derive_skill_source(normalized_request.url)
        except ValueError:
            _logger.warning(
                '[Discover] cannot derive source for %s from %s; source migration skipped',
                repo.repo_name,
                normalized_request.url,
            )
        target_platform = normalized_request.platform

        current_source = getattr(repo, 'source', None)
        current_platform = getattr(repo, 'platform', None)

        source_changed = bool(target_source) and target_source != current_source
        platform_changed = bool(target_platform) and target_platform != current_platform
        if not source_changed and not platform_changed:
            return repo

        _logger.info(
            '[Discover] migrating list source for %s: source %r -> %r, platform %r -> %r',
            repo.repo_name,
            current_source,
            target_source if source_changed else current_source,
            current_platform,
            target_platform if platform_changed else current_platform,
        )

        repo_values: dict[str, Any] = {}
        skill_values: dict[str, Any] = {}
        if source_changed:
            repo_values['source'] = target_source
            skill_values['source'] = target_source
        if platform_changed:
            repo_values['platform'] = target_platform
            skill_values['platform'] = target_platform

        await self.skill_repo_repository.update_skill_repository(
            repo.id,
            commit=False,
            **repo_values,
        )
        if skill_values:
            await self.skill_repository.session.execute(
                update(Skill)
                .where(Skill.skill_repo_id == repo.id)
                .values(**skill_values)
            )
            await self.skill_repository.session.execute(
                update(SkillVersion)
                .where(SkillVersion.skill_repo_id == repo.id)
                .values(**skill_values)
            )
        await self.skill_repository.session.commit()
        return await self.get_repository_by_id(repo.id)

    def _is_commit_unchanged(
        self,
        repo: SkillRepoModel,
        clone_dir: Path,
    ) -> bool:
        current_commit_id = self._git_ops.get_repository_head_commit_id(clone_dir)
        stored_commit_id = as_optional_str(getattr(repo, 'repository_commit_id', None))
        return bool(current_commit_id and stored_commit_id == current_commit_id)

    def _resolve_skill_author(self, platform: str | None, repo_name: str) -> str | None:
        if platform == 'community':
            return self._get_openeuler_sig_name(repo_name)
        return None

    @staticmethod
    def _count_unique_skills(skills: list[Skill]) -> int:
        return len(
            {
                str(skill.skill_id).strip()
                for skill in skills
                if getattr(skill, 'skill_id', None)
            }
        )

    async def _set_discovery_status(
        self,
        repo: SkillRepoModel,
        status: str,
    ) -> None:
        await self.skill_repo_repository.update_skill_repository(
            repo.id, skill_discover_status=status, skill_num=repo.skill_num,
        )

    @staticmethod
    def _normalize_repository_request(
        request: SkillRepositoryRequest,
    ) -> SkillRepositoryRequest:
        branch = request.branch.strip() if request.branch is not None else None
        url = (
            normalize_git_clone_url(request.url.strip())
            if request.url is not None
            else None
        )
        if not url:
            raise ValueError('git skill repos require url')
        platform = request.platform.strip() if request.platform is not None else None
        sig_name = request.sig_name.strip() if request.sig_name else None
        skill_paths = (
            [path.strip() for path in request.skill_paths if path.strip()]
            if request.skill_paths
            else None
        )
        skill_authors = (
            {path.strip(): str(val).strip() for path, val in request.skill_authors.items()
             if path.strip() and str(val).strip()}
            if request.skill_authors
            else None
        )
        skill_platforms = (
            {path.strip(): str(val).strip() for path, val in request.skill_platforms.items()
             if path.strip() and str(val).strip()}
            if request.skill_platforms
            else None
        )
        return SkillRepositoryRequest(
            branch=branch,
            url=url,
            platform=platform,
            sig_name=sig_name,
            skill_paths=skill_paths or None,
            skill_authors=skill_authors or None,
            skill_platforms=skill_platforms or None,
        )

    @staticmethod
    def _derive_repo_name(request: SkillRepositoryRequest) -> str:
        name = request.url
        for prefix in ('https://', 'http://'):
            if name.startswith(prefix):
                name = name[len(prefix):]
        name = name.removesuffix('.git')
        name = name.replace('/', '_')
        return name

    @staticmethod
    def _derive_catalog_author(request: SkillRepositoryRequest) -> str | None:
        """catalog 仓库的期望 author 口径，与扫描写入 skill_repos.author 一致。

        community 归属 SIG 目录名（sig_name）；enterprise/personal 取仓库 URL
        的 owner 段（扫描时 skill.author 的 URL 兜底口径 derive_skill_source）。
        """
        if request.platform == 'community':
            return request.sig_name
        try:
            _, owner = derive_skill_source(request.url)
            return owner
        except ValueError:
            return None

    def _get_openeuler_sig_name(self, repo_name: str) -> str | None:
        if self._openeuler_sig_by_repo_name is None:
            try:
                self._openeuler_sig_by_repo_name = build_openeuler_repo_sig_mapping()
                _logger.info(
                    "[Discover] Loaded openEuler SIG mapping: repos=%s",
                    len(self._openeuler_sig_by_repo_name),
                )
            except Exception as exc:
                _logger.warning(
                    "[Discover] Failed to load openEuler SIG mapping; continuing without SIG author: %s",
                    self._git_ops.summarize_exception(exc),
                )
                self._openeuler_sig_by_repo_name = {}
        return self._openeuler_sig_by_repo_name.get(repo_name)
