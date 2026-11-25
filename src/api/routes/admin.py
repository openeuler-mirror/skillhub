from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import require_admin_token
from src.core.database import get_db
from src.models.repository import SkillRepoRepository, SkillRepository

router = APIRouter()


@router.post(
    "/purge-soft-deleted",
    dependencies=[Depends(require_admin_token)],
)
async def purge_soft_deleted(db: AsyncSession = Depends(get_db)) -> dict:
    """物理删除已软删除（is_soft_deleted=true）的仓库与 skill 及其级联/软关联数据。

    软删除仅做隐藏标记、保留行与关联以便审计/恢复；此接口做真正的物理清理。
    """
    repo_repo = SkillRepoRepository(db)
    skill_repo = SkillRepository(db)

    repositories_purged = await repo_repo.purge_soft_deleted_skill_repositories()
    skills_purged = await skill_repo.purge_soft_deleted_skills()

    return {
        "skill_repositories_purged": repositories_purged,
        "skills_purged": skills_purged,
    }