"""Add an approximate-nearest-neighbor index for skill embeddings."""

from collections.abc import Sequence

from alembic import op

revision: str = "014_add_skill_embedding_hnsw"
down_revision: str | None = "013_contributors_git_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 修复历史 schema 漂移：部分库曾把 embedding 建成无维度 vector，
    # 无维度列不能建 hnsw/ivfflat 索引（column does not have dimensions）。
    # 列已是 vector(768) 时该语句为空操作，不重写表（实测 relfilenode 不变）。
    op.execute("ALTER TABLE skills ALTER COLUMN embedding TYPE vector(768)")
    op.execute("ALTER TABLE skill_versions ALTER COLUMN embedding TYPE vector(768)")

    op.execute(
        """
        CREATE INDEX idx_skills_embedding_hnsw
        ON skills
        USING hnsw (embedding vector_l2_ops)
        WHERE embedding IS NOT NULL
        """
    )
    # hnsw 就绪后删除 001 留下的 ivfflat 冗余索引（同一列上二者并存无查询收益，
    # 只增加写入维护成本）；skill_versions 的 ivfflat 保留（无 hnsw 版）。
    op.execute("DROP INDEX IF EXISTS idx_skills_embedding")


def downgrade() -> None:
    # 恢复 001 创建的 ivfflat 索引，再删 hnsw，保证列始终有向量索引
    op.execute(
        "CREATE INDEX idx_skills_embedding ON skills USING ivfflat (embedding vector_l2_ops)"
    )
    op.execute("DROP INDEX IF EXISTS idx_skills_embedding_hnsw")
