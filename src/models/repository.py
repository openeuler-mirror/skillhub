import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, List

from sqlalchemy import and_, case, delete, desc, func, or_, select, update
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.categories import CANONICAL_CATEGORIES, category_label
from src.models.orm import (
    Agent,
    AgentVersion,
    Contributor,
    DownloadHistory,
    SecurityAudit,
    Skill,
    SkillRepoModel,
    SkillVersion,
)

logger = logging.getLogger(__name__)


class SkillRepoRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def _coerce_repository_id(self, repository_id: str | uuid.UUID) -> uuid.UUID:
        if isinstance(repository_id, uuid.UUID):
            return repository_id
        return uuid.UUID(str(repository_id))

    async def list_skill_repositories(self) -> list[SkillRepoModel]:
        result = await self.session.execute(
            select(SkillRepoModel).order_by(SkillRepoModel.created_at.desc())
        )
        return list(result.scalars().all())

    async def create_skill_repository(
        self,
        *,
        repo_name: str,
        source: str,
        branch: str | None,
        url: str | None,
        local_path: str | None,
        skill_discover_status: str,
        repository_commit_id: str | None = None,
        platform: str | None = None,
    ) -> SkillRepoModel:
        repository = SkillRepoModel(
            repo_name=repo_name,
            source=source,
            platform=platform,
            branch=branch,
            url=url,
            local_path=local_path,
            repository_commit_id=repository_commit_id,
            skill_discover_status=skill_discover_status,
            skill_num=0,
        )
        self.session.add(repository)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(repository)
        return repository

    async def update_skill_repository(
        self,
        repository_id: str | uuid.UUID,
        *,
        source: str | None = None,
        platform: str | None = None,
        branch: str | None = None,
        url: str | None = None,
        local_path: str | None = None,
        repository_commit_id: str | None = None,
        skill_discover_status: str | None = None,
        skill_num: int | None = None,
        stars_count: int | None = None,
        forks_count: int | None = None,
        watchers_count: int | None = None,
        popularity_updated_at: datetime | None = None,
        author: str | None = None,
        commit: bool = True,
    ) -> SkillRepoModel:
        values: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
        if source is not None:
            values["source"] = source
        if platform is not None:
            values["platform"] = platform
        if branch is not None:
            values["branch"] = branch
        if url is not None:
            values["url"] = url
        if local_path is not None:
            values["local_path"] = local_path
        if repository_commit_id is not None:
            values["repository_commit_id"] = repository_commit_id
        if skill_discover_status is not None:
            values["skill_discover_status"] = skill_discover_status
        if skill_num is not None:
            values["skill_num"] = skill_num
        if stars_count is not None:
            values["stars_count"] = stars_count
        if forks_count is not None:
            values["forks_count"] = forks_count
        if watchers_count is not None:
            values["watchers_count"] = watchers_count
        if popularity_updated_at is not None:
            values["popularity_updated_at"] = popularity_updated_at
        if author is not None:
            values["author"] = author

        await self.session.execute(
            update(SkillRepoModel)
            .where(SkillRepoModel.id == self._coerce_repository_id(repository_id))
            .values(**values)
        )
        await self.session.flush()
        if commit:
            await self.session.commit()

        repository = await self.get_skill_repository_by_id(repository_id)
        if repository is None:
            raise ValueError(f"Skill repo not found: {repository_id}")
        return repository

    async def reconcile_skill_nums(self, commit: bool = True) -> int:
        """按 skills 表实际行数重算全部 skill_repos.skill_num，返回修正行数。

        口径与扫描写入的 ``unique_skill_count`` 一致（仓库下 skill 行数）。
        仅更新当前值不等于实际值的行，天然幂等；用于扫描落库后的
        统计自愈（手工删数据、历史漂移等）。
        """
        skill_count = (
            select(func.count(Skill.id))
            .where(Skill.skill_repo_id == SkillRepoModel.id)
            .scalar_subquery()
        )
        result = await self.session.execute(
            update(SkillRepoModel)
            .values(skill_num=skill_count)
            .where(
                or_(
                    SkillRepoModel.skill_num.is_(None),
                    SkillRepoModel.skill_num != skill_count,
                )
            )
            .execution_options(synchronize_session=False)
        )
        if commit:
            await self.session.commit()
        return int(result.rowcount or 0)

    async def delete_skill_repository(self, repository_id: str | uuid.UUID) -> None:
        """删除单个仓库及其级联数据（批量删除的单条便捷入口）。"""
        await self.delete_skill_repositories([repository_id])

    async def delete_skill_repositories(self, repository_ids: list[str | uuid.UUID]) -> int:
        """批量删除仓库及其级联数据，返回删除的仓库行数。

        skills / skill_versions 通过外键 CASCADE 自动删除，但
        security_audits 和 download_history 使用 resource_type + resource_id
        软关联（无外键），需要先按仓库收集 skill 与 skill_versions 的 UUID
        批量清理（security_audits 对 tag 版本同样按 resource_type='skill'
        + 版本 UUID 落记录），否则留下孤儿数据。
        """
        if not repository_ids:
            return 0
        repo_ids = [self._coerce_repository_id(item) for item in repository_ids]
        logger.info(
            "Batch delete skill repos: repo_count=%d, repo_ids=%s",
            len(repo_ids),
            repo_ids,
        )

        # 一次查出所有待删仓库下的 skill / skill_versions UUID，用于清理软关联表
        skill_ids_result = await self.session.execute(
            select(Skill.id).where(Skill.skill_repo_id.in_(repo_ids))
        )
        skill_ids = [row[0] for row in skill_ids_result.all()]
        version_ids_result = await self.session.execute(
            select(SkillVersion.id).where(SkillVersion.skill_repo_id.in_(repo_ids))
        )
        version_ids = [row[0] for row in version_ids_result.all()]

        related_deleted = 0
        resource_ids = [*skill_ids, *version_ids]
        if resource_ids:
            related_deleted = await SkillRepository(self.session).delete_skill_related_records(resource_ids)

        result = await self.session.execute(
            delete(SkillRepoModel).where(SkillRepoModel.id.in_(repo_ids))
        )
        await self.session.flush()
        await self.session.commit()
        deleted = int(result.rowcount or 0)
        logger.info(
            "Batch delete skill repos done: skill_repos_deleted=%d, "
            "related_skill_count=%d, related_records_deleted=%d",
            deleted,
            len(skill_ids),
            related_deleted,
        )
        return deleted

    async def get_skill_repository_by_id(
        self,
        repository_id: str | uuid.UUID,
    ) -> SkillRepoModel | None:
        result = await self.session.execute(
            select(SkillRepoModel).where(
                SkillRepoModel.id == self._coerce_repository_id(repository_id)
            )
        )
        return result.scalar_one_or_none()

    async def get_skill_repository_by_repo_name(
        self,
        repo_name: str,
    ) -> SkillRepoModel | None:
        result = await self.session.execute(
            select(SkillRepoModel).where(
                SkillRepoModel.repo_name == repo_name
            )
        )
        return result.scalar_one_or_none()

    async def get_skill_repository_by_url(
        self,
        url: str,
    ) -> SkillRepoModel | None:
        result = await self.session.execute(
            select(SkillRepoModel).where(SkillRepoModel.url == url)
        )
        return result.scalar_one_or_none()


class SkillRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def _version_sort_key(
        self, skill: Skill | SkillVersion
    ) -> tuple[int, tuple[int, ...], int, str, datetime, datetime]:
        version = (skill.version or "").strip()
        if version.lower() == "latest":
            return (2, tuple(), 0, "", skill.updated_at, skill.created_at)
        match = re.fullmatch(
            r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?",
            version,
            flags=re.IGNORECASE,
        )
        if match:
            major = int(match.group(1) or 0)
            minor = int(match.group(2) or 0)
            patch = int(match.group(3) or 0)
            prerelease = (match.group(4) or "").lower()
            is_stable = 1 if not prerelease else 0
            return (
                1,
                (major, minor, patch),
                is_stable,
                prerelease,
                skill.updated_at,
                skill.created_at,
            )
        return (0, tuple(), 0, "", skill.updated_at, skill.created_at)

    def _dedupe_skills(self, skills: list[Skill | SkillVersion]) -> list[Skill | SkillVersion]:
        grouped: dict[str, Skill | SkillVersion] = {}

        for skill in skills:
            skill_id = (skill.skill_id or "").strip()
            if not skill_id:
                continue

            existing = grouped.get(skill_id)
            if existing is None or self._version_sort_key(skill) > self._version_sort_key(existing):
                grouped[skill_id] = skill

        deduped = list(grouped.values())
        return sorted(
            deduped,
            key=lambda skill: self._version_sort_key(skill),
            reverse=True,
        )

    def _apply_skill_filters(
        self,
        query,
        skill_model,
        *,
        category: list[str] | None = None,
        platform: list[str] | None = None,
        tags: list[str] | None = None,
        source: str | None = None,
        skill_id_prefix: str | None = None,
        security_level: list[str] | None = None,
    ):
        # 全局规则：排除未检测（risk_score 为空）的 Skill，不进入列表
        query = query.where(skill_model.risk_score.is_not(None))
        if category:
            conditions = []
            normal_cats = []
            for c in category:
                if c.lower() == "others":
                    conditions.append(
                        or_(
                            skill_model.category.is_(None),
                            skill_model.category == "",
                            func.lower(skill_model.category).in_(["others", "other"]),
                        )
                    )
                else:
                    normal_cats.append(c)
            if normal_cats:
                conditions.append(skill_model.category.in_(normal_cats))
            if conditions:
                query = query.where(or_(*conditions))
        if platform:
            query = query.where(skill_model.platform.in_(platform))
        if tags:
            query = query.where(skill_model.tags.contains(tags))
        if source:
            query = query.where(skill_model.source == source)
        if skill_id_prefix:
            # 按仓库过滤：匹配 skill_id 前缀 {source_type}/{owner}/{repo}/
            query = query.where(skill_model.skill_id.like(f"{skill_id_prefix}/%"))
        if security_level:
            conditions = []
            for level in security_level:
                # NVIDIA SkillSpector native risk levels (0-100, higher = riskier)
                # 前端展示用中文：安全(0-20) / 低风险(21-50) / 中风险(51-80) / 高风险(81-100) / 未检测(NULL)
                if level == "安全":
                    conditions.append(skill_model.risk_score <= 20)
                elif level == "低风险":
                    conditions.append(skill_model.risk_score.between(21, 50))
                elif level == "中风险":
                    conditions.append(skill_model.risk_score.between(51, 80))
                elif level == "高风险":
                    conditions.append(skill_model.risk_score >= 81)
            if conditions:
                query = query.where(or_(*conditions))
        return query

    def _latest_unique_skills_subquery(self):
        return (
            select(Skill)
            # 全局规则：统计排除未检测（risk_score 为空）的 Skill，与列表保持一致
            .where(Skill.risk_score.is_not(None))
            .order_by(desc(Skill.updated_at), desc(Skill.created_at))
            .subquery()
        )

    async def create(self, skill_data: dict[str, Any]) -> Skill:
        skill = Skill(**skill_data)
        self.session.add(skill)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(skill)
        return skill

    async def update_download_count_by_skill_id(
        self,
        skill_id: str,
        download_count: int,
    ) -> int:
        """Set download_count for a single skill (matched by skill_id).

        Returns the number of rows updated (0 or 1).
        """
        result = await self.session.execute(
            update(Skill)
            .where(Skill.skill_id == skill_id)
            .values(download_count=download_count)
        )
        await self.session.flush()
        await self.session.commit()
        return result.rowcount or 0

    def _select_skill_by_version(
        self, skills: list[SkillVersion], version: str | None = None
    ) -> SkillVersion | None:
        if not skills:
            return None
        if len(skills) == 1:
            return skills[0]

        target_version = (version or "latest").strip()
        if target_version:
            for skill in skills:
                if (skill.version or "").strip() == target_version:
                    return skill

        return skills[0]

    async def get_by_skill_id(self, skill_id: str) -> Skill | None:
        result = await self.session.execute(
            select(Skill).where(Skill.skill_id == skill_id)
        )
        return result.scalar_one_or_none()

    async def get_with_repository_by_skill_id(self, skill_id: str) -> Skill | None:
        result = await self.session.execute(
            select(Skill)
            .options(selectinload(Skill.skill_repo))
            .where(Skill.skill_id == skill_id)
        )
        return result.scalar_one_or_none()

    async def get_version_by_skill_id_and_version(
        self,
        skill_id: str,
        version: str | None,
    ) -> SkillVersion | None:
        result = await self.session.execute(
            select(SkillVersion)
            .where(
                SkillVersion.skill_id == skill_id,
                SkillVersion.version == version,
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_version_with_repository(
        self,
        skill_id: str,
        version: str,
    ) -> SkillVersion | None:
        result = await self.session.execute(
            select(SkillVersion)
            .options(selectinload(SkillVersion.skill_repo))
            .where(
                SkillVersion.skill_id == skill_id,
                SkillVersion.version == version,
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def load_scan_records(
        self,
        skill_repo_id: uuid.UUID,
    ) -> tuple[dict[str, Skill], dict[tuple[str, str | None], SkillVersion]]:
        """Load existing latest/version records once for one repository scan."""
        skills_result = await self.session.execute(
            select(Skill).where(Skill.skill_repo_id == skill_repo_id)
        )
        versions_result = await self.session.execute(
            select(SkillVersion).where(SkillVersion.skill_repo_id == skill_repo_id)
        )
        skills = {
            skill.skill_id: skill
            for skill in skills_result.scalars().all()
            if skill.skill_id
        }
        versions = {
            (version.skill_id, version.version): version
            for version in versions_result.scalars().all()
            if version.skill_id
        }
        # 预加载是扫描前最后一次 DB 访问，之后的扫描（git 操作 + Jenkins 触发）
        # 可达数小时。若把 open transaction 横跨整个扫描，连接会长时间处于
        # idle-in-transaction 状态，被 PostgreSQL idle_in_transaction_session_timeout
        # 或 TCP 超时断开，落库阶段就会抛 "the underlying connection is closed"
        # 导致整仓扫描结果丢失。这里立即 commit 归还连接，后续取用时 pool_pre_ping
        # 会自动剔除死连接；expire_on_commit=False 保证下面返回的 ORM 记录仍可用。
        await self.session.commit()
        return skills, versions

    async def get_category_by_source_url(self, source_url: str) -> str | None:
        result = await self.session.execute(
            select(Skill.category)
            .where(
                Skill.source_url == source_url,
                Skill.category.is_not(None),
                Skill.category != "",
            )
            .order_by(desc(Skill.updated_at), desc(Skill.created_at))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_category_by_skill_id(self, skill_id: str) -> str | None:
        result = await self.session.execute(
            select(Skill.category)
            .where(
                Skill.skill_id == skill_id,
                Skill.category.is_not(None),
                Skill.category != "",
            )
            .order_by(desc(Skill.updated_at), desc(Skill.created_at))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_commit_ids_for_skill_repo(self, skill_repo_id: uuid.UUID) -> set[str]:
        result = await self.session.execute(
            select(Skill.commit_id)
            .where(
                Skill.skill_repo_id == skill_repo_id,
                Skill.commit_id.is_not(None),
                Skill.commit_id != "",
            )
        )
        return {commit_id for commit_id in result.scalars().all() if commit_id}

    async def list_by_skill_repo_id(self, skill_repo_id: uuid.UUID) -> list[Skill]:
        result = await self.session.execute(
            select(Skill).where(Skill.skill_repo_id == skill_repo_id)
        )
        return list(result.scalars().all())

    async def list_unscored_by_skill_repo(
        self,
        skill_repo_id: uuid.UUID,
    ) -> tuple[list[Skill], list[SkillVersion]]:
        skills_result = await self.session.execute(
            select(Skill).where(
                Skill.skill_repo_id == skill_repo_id,
                Skill.risk_score.is_(None),
            )
        )
        versions_result = await self.session.execute(
            select(SkillVersion).where(
                SkillVersion.skill_repo_id == skill_repo_id,
                SkillVersion.risk_score.is_(None),
            )
        )
        return (
            list(skills_result.scalars().all()),
            list(versions_result.scalars().all()),
        )

    async def store_skills_and_versions(
        self,
        skill_repo_id: uuid.UUID,
        latest_skills: list[Skill],
        tagged_skills: list[SkillVersion],
        *,
        commit: bool = True,
    ) -> tuple[list[Skill], list[SkillVersion]]:
        """Sync skills/versions for one repository, retrying once on a dead connection.

        兜底加固：长扫描期间连接可能被服务端断开（原因见 ``load_scan_records``
        的注释），落库开头就会抛 ``InterfaceError: the underlying connection is
        closed``。捕获后换连接整体重放一次：

        - ``session.close()`` 抛弃死连接和半截事务。未提交的写入（含扫描早期
          flush 的 branch/local_path 更新）本就随死连接丢失，只能回退；已加载
          的 ORM 对象变为 detached 但属性值保留，调用方后续访问不会触发
          async lazy load。session 复用时自动从连接池取新连接（pool_pre_ping
          剔除死连接）。
        - 重放复用同一 session，保持调用方 ``commit=False`` 时的单事务语义
          （与后续 SecurityAudit / Repo 更新一次 commit）。
        - 方法体按查询结果重新 diff，重放幂等。仅重试一次，再次失败照常上抛。
        """
        try:
            return await self._store_skills_and_versions_once(
                skill_repo_id, latest_skills, tagged_skills, commit=commit,
            )
        except InterfaceError as exc:
            logger.warning(
                'store_skills_and_versions hit connection error (%s); '
                'retrying once on a fresh connection',
                exc,
            )
            try:
                await self.session.close()
            except Exception:
                # 死连接上 close 本身也可能失败；连接已被连接池标记失效，
                # 继续重放即可（第二次失败会正常上抛）
                pass
            return await self._store_skills_and_versions_once(
                skill_repo_id, latest_skills, tagged_skills, commit=commit,
            )

    async def _store_skills_and_versions_once(
        self,
        skill_repo_id: uuid.UUID,
        latest_skills: list[Skill],
        tagged_skills: list[SkillVersion],
        *,
        commit: bool = True,
    ) -> tuple[list[Skill], list[SkillVersion]]:
        existing_result = await self.session.execute(
            select(Skill)
            .where(Skill.skill_repo_id == skill_repo_id)
        )
        existing_skills = {
            skill.skill_id: skill
            for skill in existing_result.scalars().all()
            if skill.skill_id
        }

        scanned_skills = {
            skill.skill_id: skill
            for skill in latest_skills
            if skill.skill_id
        }

        scanned_ids = set(scanned_skills)
        existing_ids = set(existing_skills)

        removed_ids = existing_ids - scanned_ids
        if removed_ids:
            await self.session.execute(
                delete(Skill).where(
                    Skill.skill_repo_id == skill_repo_id,
                    Skill.skill_id.in_(removed_ids),
                )
            )

        for skill_id in scanned_ids:
            skill = scanned_skills[skill_id]
            skill.skill_repo_id = skill_repo_id
            existing_skill = existing_skills.get(skill_id)
            if existing_skill is None:
                self.session.add(skill)
                continue
            skill.id = existing_skill.id
            if self._is_content_unchanged(existing_skill, skill):
                continue
            await self.session.execute(
                update(Skill)
                .where(Skill.id == existing_skill.id)
                .values(
                    **self._build_summary_update_values(
                        skill,
                        download_count=existing_skill.download_count,
                        risk_score=existing_skill.risk_score,
                    )
                )
            )

        existing_versions_result = await self.session.execute(
            select(SkillVersion).where(SkillVersion.skill_repo_id == skill_repo_id)
        )
        existing_versions = {
            (version.skill_id, version.version): version
            for version in existing_versions_result.scalars().all()
            if version.skill_id
        }
        scanned_versions = {
            (version.skill_id, version.version): version
            for version in tagged_skills
            if version.skill_id
        }

        removed_version_ids = [
            existing_versions[key].id
            for key in set(existing_versions) - set(scanned_versions)
        ]
        if removed_version_ids:
            await self.session.execute(
                delete(SkillVersion).where(SkillVersion.id.in_(removed_version_ids))
            )

        for version_key, version in scanned_versions.items():
            version.skill_repo_id = skill_repo_id
            existing_version = existing_versions.get(version_key)
            if existing_version is None:
                self.session.add(version)
                continue
            version.id = existing_version.id
            if self._is_content_unchanged(existing_version, version):
                continue
            await self.session.execute(
                update(SkillVersion)
                .where(SkillVersion.id == existing_version.id)
                .values(
                    **self._build_summary_update_values(
                        version,
                        download_count=existing_version.download_count,
                        risk_score=existing_version.risk_score,
                    )
                )
            )

        await self.session.flush()
        if commit:
            await self.session.commit()
        return latest_skills, tagged_skills

    @staticmethod
    def _is_content_unchanged(
        existing: Skill | SkillVersion,
        scanned: Skill | SkillVersion,
    ) -> bool:
        """Return whether a scanned record is content-identical to the stored one.

        The directory ``tree_hash`` is the primary signal; when it is missing on
        either side we fall back to comparing ``commit_id``.
        """
        if existing.tree_hash is not None and scanned.tree_hash is not None:
            return existing.tree_hash == scanned.tree_hash
        return existing.commit_id == scanned.commit_id

    def _build_summary_update_values(
        self,
        representative: Skill | SkillVersion,
        *,
        download_count: int,
        risk_score: Any,
    ) -> dict[str, Any]:
        """内容变化时的整行更新值。

        ``download_count`` / ``risk_score`` 保留已有值：前者由 popularity 任务
        维护、后者由安全审计（Jenkins 收集器）回写，均不是本次扫描的产物，
        用新解析对象的默认值（None）覆盖会丢失已完成的审计分数。
        """
        return {
            "skill_repo_id": representative.skill_repo_id,
            "skill_id": representative.skill_id,
            "name": representative.name,
            "description": representative.description,
            "version": representative.version,
            "commit_id": representative.commit_id,
            "tree_hash": representative.tree_hash,
            "author": representative.author,
            "source": representative.source,
            "source_url": representative.source_url,
            "repo_url": representative.repo_url,
            "category": representative.category,
            "tags": representative.tags,
            "platform": representative.platform,
            "extra_metadata": representative.extra_metadata,
            "content": representative.content,
            "risk_score": risk_score,
            "download_count": download_count,
            "rating": representative.rating,
            "updated_at": datetime.now(timezone.utc),
            "last_indexed_at": None,
            "embedding": None,
        }

    async def get_versions_by_base_skill(self, source_url: str | None, skill_id: str) -> list[SkillVersion]:
        query = select(SkillVersion)
        query = query.where(SkillVersion.skill_id == skill_id)
        if source_url:
            query = query.where(SkillVersion.source_url == source_url)
        query = query.order_by(desc(SkillVersion.updated_at), desc(SkillVersion.created_at))
        result = await self.session.execute(query)
        skills = list(result.scalars().all())
        return sorted(skills, key=self._version_sort_key, reverse=True)

    async def get_by_id(self, id: uuid.UUID) -> Skill | None:
        result = await self.session.execute(
            select(Skill).where(Skill.id == id)
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        skip: int = 0,
        limit: int = 20,
        category: list[str] | None = None,
        platform: list[str] | None = None,
        tags: list[str] | None = None,
        source: str | None = None,
        skill_id_prefix: str | None = None,
        sort_by: str = "updated_at",
        sort_period: str | None = None,
        security_level: list[str] | None = None,
    ) -> tuple[list[Skill], int]:
        filtered_query = self._apply_skill_filters(
            select(Skill),
            Skill,
            category=category,
            platform=platform,
            tags=tags,
            source=source,
            skill_id_prefix=skill_id_prefix,
            security_level=security_level,
        )

        count_query = self._apply_skill_filters(
            select(func.count(Skill.id)),
            Skill,
            category=category,
            platform=platform,
            tags=tags,
            source=source,
            skill_id_prefix=skill_id_prefix,
            security_level=security_level,
        )
        rank_by_period = sort_by == "download_count" and sort_period in ("week", "month")

        if rank_by_period:
            now = datetime.now(timezone.utc)
            if sort_period == "week":
                cutoff = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                cutoff = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            dl_subquery = (
                select(
                    DownloadHistory.resource_id,
                    func.count(DownloadHistory.id).label("period_downloads"),
                )
                .where(DownloadHistory.resource_type == "skill")
                .where(DownloadHistory.downloaded_at >= cutoff)
                .group_by(DownloadHistory.resource_id)
                .subquery()
            )
            # 热门-本周/本月：仅展示该周期内有下载的 Skill（inner join 天然排除 0），
            # 列表查询与计数查询保持同一口径
            filtered_query = filtered_query.join(
                dl_subquery, Skill.id == dl_subquery.c.resource_id
            )
            count_query = count_query.join(
                dl_subquery, Skill.id == dl_subquery.c.resource_id
            )
        # 热门-全部时间：不再过滤 download_count > 0，全部可见 Skill 按下载量排序
        #（0 下载自然沉底），保证默认态总数与顶部统计（total_skills）一致

        total = await self.session.scalar(count_query)

        if rank_by_period:
            query = (
                filtered_query
                .add_columns(dl_subquery.c.period_downloads)
                .order_by(
                    desc(dl_subquery.c.period_downloads),
                    desc(Skill.updated_at),
                    desc(Skill.created_at),
                )
                .offset(skip)
                .limit(limit)
            )
        elif sort_by == "download_count":
            order_by = [desc(Skill.download_count), desc(Skill.updated_at), desc(Skill.created_at)]
            query = filtered_query.order_by(*order_by).offset(skip).limit(limit)
        else:
            order_by = [desc(Skill.updated_at), desc(Skill.created_at)]
            query = filtered_query.order_by(*order_by).offset(skip).limit(limit)

        result = await self.session.execute(query)
        if rank_by_period:
            # 周期排序：查询结果同时包含 Skill 实体与周期下载量，挂载到实体上返回
            skills = []
            for row in result.all():
                skill, period_downloads = row[0], row[1] or 0
                skill._period_downloads = period_downloads
                skills.append(skill)
        else:
            skills = list(result.scalars().all())

        return skills, total or 0

    async def get_contributor_skills(
        self,
        source: str,
        author: str,
        skip: int = 0,
        limit: int = 20,
        sort_by: str = "download_count",
        keyword: str | None = None,
    ) -> tuple[List[Skill], int, int, str | None, int]:
        """查询 contributor（source + author）下的全部可见 skills 及聚合统计。

        Returns:
            (skills, total, total_downloads, platform, skill_count)：
            total 受 keyword 过滤（分页用）；skill_count / total_downloads 始终
            为全量口径（hero 统计卡展示，不随搜索变化）。skill_count 为 0
            表示该 contributor 名下没有可见 skill（路由层转 404）。
        """
        base_filter = and_(
            Skill.source == source,
            Skill.author == author,
            Skill.risk_score.is_not(None),
        )

        stats_result = await self.session.execute(
            select(
                func.count(Skill.id),
                func.coalesce(func.sum(Skill.download_count), 0),
                func.min(Skill.platform),
            ).where(base_filter)
        )
        skill_count, total_downloads, platform = stats_result.one()

        if not skill_count:
            return [], 0, 0, None, 0

        total = int(skill_count)
        query_filter: Any = base_filter
        if keyword and keyword.strip():
            kw = f"%{keyword.strip()}%"
            query_filter = and_(
                base_filter,
                or_(
                    Skill.name.ilike(kw),
                    Skill.description.ilike(kw),
                    func.array_to_string(Skill.tags, " ").ilike(kw),
                ),
            )
            total = int(
                (await self.session.execute(
                    select(func.count(Skill.id)).where(query_filter)
                )).scalar()
                or 0
            )

        query = select(Skill).where(query_filter)
        if sort_by == "download_count":
            order_by = [desc(Skill.download_count), desc(Skill.updated_at), desc(Skill.created_at)]
        else:
            order_by = [desc(Skill.updated_at), desc(Skill.created_at)]
        query = query.order_by(*order_by).offset(skip).limit(limit)

        result = await self.session.execute(query)
        skills = list(result.scalars().all())
        return skills, total, int(total_downloads), platform, int(skill_count)

    async def update(self, skill_id: str, update_data: dict[str, Any]) -> Skill | None:
        existing = await self.get_by_skill_id(skill_id)
        if existing is None:
            return None
        await self.session.execute(
            update(Skill).where(Skill.id == existing.id).values(**update_data)
        )
        await self.session.flush()
        return await self.get_by_skill_id(skill_id)

    async def delete_skill_related_records(self, skill_ids: List[uuid.UUID]) -> int:
        """清理与 skill 软关联的 security_audits 和 download_history 记录。

        这两张表通过 resource_type='skill' + resource_id=skill.id 关联，
        无外键约束，删 skill 时不会自动级联删除。在删除单个 skill 或
        批量删除仓库下所有 skill 时都应调用此方法，避免留下孤儿数据。
        返回清理掉的记录总行数（供调用方日志展示）。
        """
        if not skill_ids:
            return 0
        audit_result = await self.session.execute(
            delete(SecurityAudit).where(
                SecurityAudit.resource_type == "skill",
                SecurityAudit.resource_id.in_(skill_ids),
            )
        )
        history_result = await self.session.execute(
            delete(DownloadHistory).where(
                DownloadHistory.resource_type == "skill",
                DownloadHistory.resource_id.in_(skill_ids),
            )
        )
        await self.session.flush()
        return int(audit_result.rowcount or 0) + int(history_result.rowcount or 0)

    async def delete(self, skill_id: str) -> bool:
        # 先查出 skill 与其 tag 版本的 UUID，用于清理软关联表
        # （security_audits 对 tag 版本同样按 resource_type='skill' + 版本 UUID 落记录）
        id_result = await self.session.execute(
            select(Skill.id).where(Skill.skill_id == skill_id)
        )
        skill_uuid = id_result.scalar_one_or_none()
        version_ids_result = await self.session.execute(
            select(SkillVersion.id).where(SkillVersion.skill_id == skill_id)
        )
        version_ids = [row[0] for row in version_ids_result.all()]

        version_result = await self.session.execute(
            delete(SkillVersion).where(SkillVersion.skill_id == skill_id)
        )
        summary_result = await self.session.execute(
            delete(Skill).where(Skill.skill_id == skill_id)
        )
        await self.session.flush()

        # 清理 security_audits 和 download_history 中的孤儿数据
        related_ids = [*([skill_uuid] if skill_uuid else []), *version_ids]
        if related_ids:
            await self.delete_skill_related_records(related_ids)

        return (version_result.rowcount or 0) > 0 or (summary_result.rowcount or 0) > 0

    async def delete_skills_not_in_skill_ids(
        self, skill_repo_id: uuid.UUID, expected_skill_ids: set[str],
    ) -> int:
        """删除仓库下 skill_id 不在期望集合内的 skill 及其版本，返回删除的 skill 行数。

        与 ``store_skills_and_versions`` 的 diff 删除同口径，但额外清理
        security_audits / download_history 软关联（store 的 diff 删除不清理，
        会留下孤儿数据）。用于扫描前的「同步删除」：catalog 中撤销登记的
        ``skills[].skill_url`` / ``local_skills`` 条目对应的 skill 据此删除，
        即使远端仓库 commit 未变化（``discover`` 的 commit 短路会跳过扫描）。
        """
        if not expected_skill_ids:
            return 0
        removed_result = await self.session.execute(
            select(Skill.id).where(
                Skill.skill_repo_id == skill_repo_id,
                Skill.skill_id.not_in(expected_skill_ids),
            )
        )
        removed_uuids = [row[0] for row in removed_result.all()]
        version_ids_result = await self.session.execute(
            select(SkillVersion.id).where(
                SkillVersion.skill_repo_id == skill_repo_id,
                SkillVersion.skill_id.not_in(expected_skill_ids),
            )
        )
        version_ids = [row[0] for row in version_ids_result.all()]

        if not removed_uuids and not version_ids:
            return 0

        if removed_uuids:
            await self.session.execute(
                delete(Skill).where(Skill.id.in_(removed_uuids))
            )
        if version_ids:
            await self.session.execute(
                delete(SkillVersion).where(SkillVersion.id.in_(version_ids))
            )
        await self.session.flush()

        # 清理 security_audits / download_history 软关联（无外键，不会级联）
        await self.delete_skill_related_records([*removed_uuids, *version_ids])
        await self.session.commit()
        return len(removed_uuids)

    async def increment_download(self, skill_id: str) -> uuid.UUID | None:
        """累加下载计数，返回该 Skill 的主键 UUID（未命中返回 None），供调用方落 DownloadHistory。"""
        existing = await self.get_by_skill_id(skill_id)
        if existing is None:
            return None
        await self.session.execute(
            update(Skill)
            .where(Skill.id == existing.id)
            .values(download_count=Skill.download_count + 1)
        )
        await self.session.flush()
        return existing.id

    async def update_last_indexed(self, skill_id: str) -> None:
        existing = await self.get_by_skill_id(skill_id)
        if existing is None:
            return
        await self.session.execute(
            update(Skill)
            .where(Skill.id == existing.id)
            .values(last_indexed_at=datetime.now(timezone.utc))
        )
        await self.session.flush()

    async def update_embedding(self, skill_id: str, embedding: List[float]) -> None:
        existing = await self.get_by_skill_id(skill_id)
        if existing is None:
            return
        await self.session.execute(
            update(Skill)
            .where(Skill.id == existing.id)
            .values(embedding=embedding, last_indexed_at=datetime.now(timezone.utc))
        )
        await self.session.flush()
        await self.session.commit()

    async def update_embeddings_batch(
        self, embeddings: List[tuple[str, datetime, List[float]]]
    ) -> int:
        """仅在记录版本未变化时批量写入 embedding，返回更新行数。"""
        now = datetime.now(timezone.utc)
        count = 0
        for skill_id, expected_updated_at, embedding in embeddings:
            result = await self.session.execute(
                update(Skill)
                .where(
                    Skill.skill_id == skill_id,
                    Skill.updated_at == expected_updated_at,
                )
                .values(embedding=embedding, last_indexed_at=now)
            )
            count += result.rowcount or 0
        await self.session.flush()
        await self.session.commit()
        return count

    async def get_stats(self) -> dict[str, Any]:
        latest_skills = self._latest_unique_skills_subquery()

        total_result = await self.session.execute(
            select(func.count()).select_from(latest_skills)
        )
        total_skills = total_result.scalar() or 0

        raw_category_key = func.lower(
            func.nullif(func.trim(latest_skills.c.category), "")
        )
        normalized_category = case(
            (raw_category_key.is_(None), "others"),
            (raw_category_key.in_(["other", "others"]), "others"),
            else_=raw_category_key,
        )
        category_result = await self.session.execute(
            select(
                normalized_category.label("category_key"),
                func.min(latest_skills.c.category).label("display_name"),
                func.count().label("count"),
            )
            .select_from(latest_skills)
            .group_by(normalized_category)
            .order_by(func.count().desc())
        )
        # Build category count map from DB (normalize "others" key)
        categories_map: dict[str, int] = {}
        for row in category_result.fetchall():
            category_key = row.category_key
            if category_key in {None, "other", "others"}:
                key = "others"
            else:
                key = row.display_name or "others"
            categories_map[key] = row.count

        # Always return all canonical categories, filling 0 for missing values.
        canonical_categories = CANONICAL_CATEGORIES
        categories = [
            {
                "name": name,
                "label": category_label(name),
                "count": categories_map.get(name, 0),
            }
            for name in canonical_categories
        ]
        # Sort: non-others by count desc, others at end
        others_entry = [c for c in categories if c["name"] == "others"]
        non_others = sorted(
            [c for c in categories if c["name"] != "others"],
            key=lambda x: -x["count"],
        )
        categories = non_others + others_entry
        total_categories = len(non_others)

        # Platform counts
        platform_result = await self.session.execute(
            select(
                latest_skills.c.platform,
                func.count().label("count"),
            )
            .select_from(latest_skills)
            .where(latest_skills.c.platform.is_not(None))
            .where(latest_skills.c.platform != "")
            .group_by(latest_skills.c.platform)
            .order_by(func.count().desc())
        )
        platforms = [{"name": row.platform, "count": row.count} for row in platform_result.fetchall()]

        # Security level counts (NVIDIA SkillSpector native levels, 中文展示)
        # 安全(0-20) / 低风险(21-50) / 中风险(51-80) / 高风险(81-100) / 未检测(NULL)
        level_expr = case(
            (latest_skills.c.risk_score <= 20, "安全"),
            (latest_skills.c.risk_score.between(21, 50), "低风险"),
            (latest_skills.c.risk_score.between(51, 80), "中风险"),
            (latest_skills.c.risk_score >= 81, "高风险"),
            else_="未检测",
        )
        security_result = await self.session.execute(
            select(
                level_expr.label("level"),
                func.count().label("count"),
            )
            .select_from(latest_skills)
            .group_by(level_expr)
            .order_by(
                case(
                    (level_expr == "安全", 1),
                    (level_expr == "低风险", 2),
                    (level_expr == "中风险", 3),
                    (level_expr == "高风险", 4),
                    else_=5,
                )
            )
        )
        security_levels = [{"name": row.level, "count": row.count} for row in security_result.fetchall()]

        # 下载总量
        downloads_result = await self.session.execute(
            select(func.coalesce(func.sum(latest_skills.c.download_count), 0))
            .select_from(latest_skills)
        )
        total_downloads = downloads_result.scalar() or 0

        return {
            "total_skills": total_skills,
            "total_categories": total_categories,
            "total_downloads": total_downloads,
            "categories": categories,
            "platforms": platforms,
            "security_levels": security_levels,
        }


class ContributorRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(
        self,
        *,
        source: str,
        author: str,
        platform: str | None = None,
        name: str | None = None,
        description: str | None = None,
        git_profile: str | None = None,
        website: str | None = None,
        repo_url: str | None = None,
        skill_count: int | None = None,
        commit: bool = True,
    ) -> Contributor:
        """Insert or update a contributor row.

        On conflict (source, author): platform/repo_url/skill_count are always
        refreshed; profile fields (name/description/git_profile/website) are
        refreshed only when the caller provides a non-empty value (catalog is
        the source of truth when it provides one; null keeps the existing
        value, so manual enrichment of fields the catalog does not carry is
        preserved).
        """
        existing = await self.session.execute(
            select(Contributor).where(
                Contributor.source == source,
                Contributor.author == author,
            )
        )
        row = existing.scalar_one_or_none()

        if row is None:
            row = Contributor(
                source=source,
                author=author,
                platform=platform,
                name=name or author,
                description=description,
                git_profile=git_profile,
                website=website,
                repo_url=repo_url,
                skill_count=skill_count or 0,
            )
            self.session.add(row)
        else:
            if platform is not None:
                row.platform = platform
            if name:
                row.name = name
            if description:
                row.description = description
            if git_profile:
                row.git_profile = git_profile
            if website:
                row.website = website
            if repo_url is not None:
                row.repo_url = repo_url
            if skill_count is not None:
                row.skill_count = skill_count
            row.updated_at = datetime.now(timezone.utc)

        if commit:
            await self.session.commit()
        return row

    async def get(self, source: str, author: str) -> Contributor | None:
        result = await self.session.execute(
            select(Contributor).where(
                Contributor.source == source,
                Contributor.author == author,
            )
        )
        return result.scalar_one_or_none()

    async def count_skills(self, source: str, author: str) -> int:
        """统计贡献者名下可见 skill 数（含同 author 的其他仓库）。

        可见性规则与 ``SkillRepository.get_contributor_skills`` 一致
        （risk_score 已审计非空）。同一事务内可看到本次扫描未 commit 的写入，
        因此扫描流程 store_skills_and_versions 之后调用即可拿到最新值。
        """
        result = await self.session.execute(
            select(func.count(Skill.id)).where(
                Skill.source == source,
                Skill.author == author,
                Skill.risk_score.is_not(None),
            )
        )
        return int(result.scalar() or 0)

    async def reconcile_skill_counts(self, commit: bool = True) -> int:
        """按 skills 表可见口径重算全部 contributors.skill_count，返回修正行数。

        可见性规则与 ``count_skills`` / ``get_contributor_skills`` 一致
        （risk_score 非空），与扫描 upsert 时的聚合口径相同，不会在
        扫描与自愈之间来回翻转。仅更新当前值不等于聚合值的行，
        天然幂等；用于扫描落库后的统计自愈（手工删数据、历史漂移等）。
        """
        visible_count = (
            select(func.count(Skill.id))
            .where(
                Skill.source == Contributor.source,
                Skill.author == Contributor.author,
                Skill.risk_score.is_not(None),
            )
            .scalar_subquery()
        )
        result = await self.session.execute(
            update(Contributor)
            .values(skill_count=visible_count)
            .where(
                or_(
                    Contributor.skill_count.is_(None),
                    Contributor.skill_count != visible_count,
                )
            )
            .execution_options(synchronize_session=False)
        )
        if commit:
            await self.session.commit()
        return int(result.rowcount or 0)

    async def delete_contributors_without_skills(self, commit: bool = True) -> int:
        """物理删除名下已无任何 skill 记录的 contributor，返回删除行数。

        判定口径为 skills 表中 (source, author) 是否存在任何行（而非
        ``reconcile_skill_counts`` 的可见口径——后者会把仅有未审计 skill 的
        贡献者也算作空并误删）。用于 catalog 删除仓库后清理孤儿作者，幂等。
        """
        skill_exists = (
            select(func.count(Skill.id))
            .where(
                Skill.source == Contributor.source,
                Skill.author == Contributor.author,
            )
            .scalar_subquery()
        )
        result = await self.session.execute(
            delete(Contributor).where(skill_exists == 0)
        )
        if commit:
            await self.session.commit()
        return int(result.rowcount or 0)

    async def list(
        self,
        *,
        skip: int = 0,
        limit: int = 20,
        platform: str | None = None,
        keyword: str | None = None,
        sort_by: str = "skill_count",
    ) -> tuple[list[tuple[Contributor, int]], int, dict[str, int], int]:
        """Return ((contributor, total_downloads) rows, total, platform_counts, grand_total).

        ``total_downloads`` aggregates ``skills.download_count`` per
        (source, author) via a correlated subquery (same visibility rule as
        ``SkillRepository.get_contributor_skills``: risk_score not null).

        ``platform_counts`` counts contributors per platform under the same
        keyword filter (ignoring ``platform`` tab selection so the tabs always
        reflect the full search scope).

        ``grand_total`` is the full-scope count under the current keyword only
        (ignoring ``platform``); used for the "全部" tab so its count stays
        stable across platform switches.

        Contributors with ``skill_count`` = 0 (no visible skill under their
        name, e.g. after low-value cleanup or audit-pending skills) are
        excluded from rows and all counts — they carry no browsable content.
        """
        visible = Contributor.skill_count > 0
        base_filter: list[Any] = [visible]
        count_filter: list[Any] = [visible]
        if platform:
            base_filter.append(Contributor.platform == platform)
        if keyword:
            kw = f"%{keyword.strip()}%"
            like = or_(
                Contributor.name.ilike(kw),
                Contributor.author.ilike(kw),
                Contributor.description.ilike(kw),
            )
            base_filter.append(like)
            count_filter.append(like)

        # ``total`` 反映当前筛选（含 platform）下的总数，用于分页；
        # ``grand_total`` 仅按 keyword 聚合（忽略 platform），"全部" tab 计数；
        # ``platform_counts`` 仅按 keyword 聚合（忽略 platform），各 tab 计数。
        count_q = select(func.count(Contributor.id))
        if base_filter:
            count_q = count_q.where(*base_filter)
        total = (await self.session.execute(count_q)).scalar() or 0

        grand_q = select(func.count(Contributor.id))
        if count_filter:
            grand_q = grand_q.where(*count_filter)
        grand_total = (await self.session.execute(grand_q)).scalar() or 0

        # Per-platform counts under current keyword filter
        pc_q = (
            select(Contributor.platform, func.count(Contributor.id))
            .where(*count_filter)
            .group_by(Contributor.platform)
        )
        pc_rows = (await self.session.execute(pc_q)).all()
        platform_counts: dict[str, int] = {}
        for p, c in pc_rows:
            if p:
                platform_counts[p] = int(c)

        # Correlated subquery: total downloads across the contributor's skills
        downloads_sq = (
            select(func.coalesce(func.sum(Skill.download_count), 0))
            .where(
                Skill.source == Contributor.source,
                Skill.author == Contributor.author,
                Skill.risk_score.is_not(None),
            )
            .correlate(Contributor)
            .scalar_subquery()
        )

        q = select(Contributor, downloads_sq.label("total_downloads"))
        if base_filter:
            q = q.where(*base_filter)
        if sort_by == "created_at":
            q = q.order_by(desc(Contributor.created_at))
        else:
            q = q.order_by(desc(Contributor.skill_count), desc(Contributor.created_at))
        q = q.offset(skip).limit(limit)

        result = await self.session.execute(q)
        rows = [(row[0], int(row[1] or 0)) for row in result.all()]
        return rows, total, platform_counts, grand_total


class AgentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, agent_data: dict[str, Any]) -> Agent:
        agent = Agent(**agent_data)
        self.session.add(agent)
        await self.session.flush()
        await self.session.refresh(agent)
        return agent

    async def get_by_agent_id(self, agent_id: str) -> Agent | None:
        result = await self.session.execute(
            select(Agent).where(Agent.agent_id == agent_id)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, id: uuid.UUID) -> Agent | None:
        result = await self.session.execute(
            select(Agent).where(Agent.id == id)
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        skip: int = 0,
        limit: int = 20,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> tuple[list[Agent], int]:
        query = select(Agent)

        if category:
            query = query.where(Agent.category == category)
        if tags:
            query = query.where(Agent.tags.contains(tags))

        count_query = select(func.count()).select_from(query.subquery())
        total = await self.session.scalar(count_query)

        query = query.offset(skip).limit(limit).order_by(Agent.created_at.desc())
        result = await self.session.execute(query)
        agents = list(result.scalars().all())

        return agents, total or 0

    async def get_versions(self, agent_id: uuid.UUID) -> List[AgentVersion]:
        result = await self.session.execute(
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent_id)
            .order_by(AgentVersion.released_at.desc())
        )
        return list(result.scalars().all())

    async def create_version(self, version_data: dict[str, Any]) -> AgentVersion:
        version = AgentVersion(**version_data)
        self.session.add(version)
        await self.session.flush()
        await self.session.refresh(version)
        return version

    async def increment_download(self, agent_id: uuid.UUID) -> None:
        await self.session.execute(
            update(Agent)
            .where(Agent.id == agent_id)
            .values(download_count=Agent.download_count + 1)
        )
        await self.session.flush()

    async def delete(self, agent_id: str) -> bool:
        result = await self.session.execute(
            delete(Agent).where(Agent.agent_id == agent_id)
        )
        await self.session.flush()
        return result.rowcount > 0


class SecurityAuditRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, audit_data: dict[str, Any]) -> SecurityAudit:
        audit = SecurityAudit(**audit_data)
        self.session.add(audit)
        await self.session.flush()
        await self.session.refresh(audit)
        return audit

    async def get_latest_by_resource(
        self, resource_type: str, resource_id: uuid.UUID, for_update: bool = False
    ) -> SecurityAudit | None:
        stmt = (
            select(SecurityAudit)
            .where(
                SecurityAudit.resource_type == resource_type,
                SecurityAudit.resource_id == resource_id,
            )
            .order_by(SecurityAudit.audited_at.desc())
            .limit(1)
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_resource(
        self, resource_type: str, resource_id: uuid.UUID
    ) -> list[SecurityAudit]:
        result = await self.session.execute(
            select(SecurityAudit)
            .where(
                SecurityAudit.resource_type == resource_type,
                SecurityAudit.resource_id == resource_id,
            )
            .order_by(SecurityAudit.audited_at.desc())
        )
        return list(result.scalars().all())

    async def upsert_by_resource(
        self,
        resource_type: str,
        resource_id: uuid.UUID,
        audit_data: dict[str, Any],
    ) -> SecurityAudit:
        """Create or update a SecurityAudit record for the given resource.

        One record per (resource_type, resource_id) — if one exists, its
        fields are updated and the ``collected`` flag is reset so the
        background collector picks it up again.
        """
        existing = await self.get_latest_by_resource(resource_type, resource_id, for_update=True)
        if existing is not None:
            for key, value in audit_data.items():
                setattr(existing, key, value)
            if existing.details:
                existing.details.pop('skillspector_collected', None)
            await self.session.flush()
            await self.session.refresh(existing)
            return existing
        audit = SecurityAudit(**audit_data)
        self.session.add(audit)
        await self.session.flush()
        await self.session.refresh(audit)
        return audit

    async def upsert_many_by_resource(
        self,
        resource_type: str,
        entries: list[tuple[uuid.UUID, dict[str, Any]]],
    ) -> list[SecurityAudit]:
        """Batch create/update SecurityAudit rows for one resource type.

        Mirrors ``upsert_by_resource`` semantics — update an existing row's
        fields and reset ``skillspector_collected`` — but issues a single
        lookup and a single flush instead of one round-trip per row.
        """
        if not entries:
            return []

        resource_ids = [resource_id for resource_id, _ in entries]
        result = await self.session.execute(
            select(SecurityAudit)
            .where(
                SecurityAudit.resource_type == resource_type,
                SecurityAudit.resource_id.in_(resource_ids),
            )
            .with_for_update()
        )
        existing_by_id = {
            audit.resource_id: audit for audit in result.scalars().all()
        }

        audits: list[SecurityAudit] = []
        for resource_id, audit_data in entries:
            existing = existing_by_id.get(resource_id)
            if existing is not None:
                for key, value in audit_data.items():
                    setattr(existing, key, value)
                if existing.details:
                    existing.details.pop('skillspector_collected', None)
                audits.append(existing)
            else:
                audit = SecurityAudit(**audit_data)
                self.session.add(audit)
                audits.append(audit)

        await self.session.flush()
        return audits


class DownloadHistoryRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, download_data: dict[str, Any]) -> DownloadHistory:
        record = DownloadHistory(**download_data)
        self.session.add(record)
        await self.session.flush()
        await self.session.refresh(record)
        return record
