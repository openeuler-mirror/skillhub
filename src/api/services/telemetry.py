import json

from sqlalchemy.ext.asyncio import AsyncSession

from src.skills.skill_id import build_skill_id, extract_owner_repo, slugify_identifier

from src.models.orm import DownloadHistory
from src.models.repository import SkillRepository


def build_skill_id_from_telemetry(
    source_type: str | None,
    repo_url: str | None,
    skill_name: str,
    skill_files: dict[str, str] | None = None,
) -> str | None:
    if source_type == "gitcode":
        source_type = "atomgit"
    if source_type not in {"github", "atomgit", "gitlab", "gitee"}:
        return None
    if not repo_url:
        return None

    try:
        owner_repo = extract_owner_repo(repo_url)
    except ValueError:
        return None

    # Prefer the repo-relative SKILL.md path from the CLI.  This uses the
    # exact same derivation as the crawler (build_skill_id), so install
    # telemetry/audit lookups hit the same skill_id in the database.
    if skill_files:
        relative_path = skill_files.get(skill_name)
        if relative_path:
            normalized_path = relative_path.strip().replace("\\", "/").rstrip("/")
            try:
                return build_skill_id(source_type, owner_repo, normalized_path)
            except ValueError:
                pass

    # No path info available — guess the SKILL.md path from the skill name.
    skill_slug = slugify_identifier(skill_name)
    if not skill_slug:
        return None
    try:
        return build_skill_id(source_type, owner_repo, f"{skill_slug}/SKILL.md")
    except ValueError:
        return None


class TelemetryService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.skill_repo = SkillRepository(session)

    async def process(
        self,
        params: dict[str, str],
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> list[str]:
        if params.get("event") != "install":
            return []

        return await self._process_install(params, ip_address=ip_address, user_agent=user_agent)

    async def _process_install(
        self,
        params: dict[str, str],
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> list[str]:
        repo_url = params.get("source")
        source_type = params.get("sourceType")
        skills = [skill.strip() for skill in params.get("skills", "").split(",") if skill.strip()]

        skill_files: dict[str, str] | None = None
        raw_skill_files = params.get("skillFiles")
        if raw_skill_files:
            try:
                parsed_skill_files = json.loads(raw_skill_files)
            except json.JSONDecodeError:
                parsed_skill_files = None
            if isinstance(parsed_skill_files, dict):
                skill_files = {
                    str(key): str(value)
                    for key, value in parsed_skill_files.items()
                    if isinstance(key, str) and isinstance(value, str)
                }

        matched_skill_ids: list[str] = []
        for skill_name in skills:
            skill_id = build_skill_id_from_telemetry(source_type, repo_url, skill_name, skill_files)
            if not skill_id:
                continue
            skill_uuid = await self.skill_repo.increment_download(skill_id)
            if skill_uuid is None:
                continue
            # 与 API 下载路径对齐：落 DownloadHistory 记录，供本周/本月下载量排序统计
            self.session.add(
                DownloadHistory(
                    resource_type="skill",
                    resource_id=skill_uuid,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
            )
            matched_skill_ids.append(skill_id)

        if matched_skill_ids:
            await self.session.commit()

        return matched_skill_ids
