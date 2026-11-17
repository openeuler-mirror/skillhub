from abc import ABC, abstractmethod
from collections import OrderedDict
import re
import time
from typing import Any

import httpx
from src.core.config import get_settings


# BGE v1.5 的 query-side instruction，文档向量不添加该前缀。
# 按查询语言选择指令：中文查询用中文指令，其余（主要为英文）用英文指令。
BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："  # 中文查询指令
BGE_QUERY_INSTRUCTION_EN = "Represent this sentence for searching relevant passages: "  # 英文查询指令
QUERY_CACHE_TTL_SECONDS = 300.0
QUERY_CACHE_MAX_ENTRIES = 256


class EmbeddingProvider(ABC):
    @abstractmethod
    async def encode(self, texts: list[str]) -> list[list[float]]:
        pass

    @abstractmethod
    def dimension(self) -> int:
        pass


class LocalEmbeddingService(EmbeddingProvider):
    def __init__(self, host: str, model: str, dimension: int):
        self.host = host
        self.model = model
        self._dimension = dimension
        # 200s：实测 batch=8 在 CPU 受限的部署环境单批约 80s（~25s 固定开销
        # + ~7s/条），留出倍级余量；批再大则可能逼近超时、整批被客户端放弃，
        # 需二分重试降级。配合 BATCH_SIZE=8 使用。
        self._client = httpx.AsyncClient(timeout=200.0)

    async def encode(self, texts: list[str]) -> list[list[float]]:
        response = await self._client.post(
            f"{self.host}/v1/embeddings",
            json={"input": texts, "model": self.model},
        )
        response.raise_for_status()
        data = response.json()
        return [item["embedding"] for item in data["data"]]

    async def close(self) -> None:
        await self._client.aclose()

    def dimension(self) -> int:
        return self._dimension


class MockEmbeddingService(EmbeddingProvider):
    """Mock embedding service for testing without actual model."""

    def __init__(self, dimension: int = 768):
        self._dimension = dimension

    async def encode(self, texts: list[str]) -> list[list[float]]:
        import random

        return [
            [random.random() for _ in range(self._dimension)]
            for _ in texts
        ]

    def dimension(self) -> int:
        return self._dimension


_embedding_service: EmbeddingProvider | None = None
_query_embedding_cache: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()


def get_embedding_service() -> EmbeddingProvider:
    global _embedding_service
    if _embedding_service is None:
        settings = get_settings()
        if settings.ai.enable_semantic_search:
            _embedding_service = LocalEmbeddingService(
                host=settings.ai.embedding_host,
                model=settings.ai.embedding_model,
                dimension=settings.ai.embedding_dimension,
            )
        else:
            _embedding_service = MockEmbeddingService()
    return _embedding_service


async def generate_embeddings(texts: list[str]) -> list[list[float]]:
    service = get_embedding_service()
    return await service.encode(texts)


async def generate_query_embedding(query: str) -> list[float] | None:
    """生成带 BGE 查询指令的向量，并缓存短期重复查询。"""
    normalized_query = query.strip()
    if not normalized_query:
        return None

    now = time.monotonic()
    cached = _query_embedding_cache.get(normalized_query)
    if cached is not None:
        cached_at, embedding = cached
        if now - cached_at < QUERY_CACHE_TTL_SECONDS:
            _query_embedding_cache.move_to_end(normalized_query)
            return embedding
        _query_embedding_cache.pop(normalized_query, None)

    embeddings = await generate_embeddings([prepare_search_query(normalized_query)])
    embedding = embeddings[0] if embeddings else None
    if embedding is not None:
        _query_embedding_cache[normalized_query] = (now, embedding)
        _query_embedding_cache.move_to_end(normalized_query)
        while len(_query_embedding_cache) > QUERY_CACHE_MAX_ENTRIES:
            _query_embedding_cache.popitem(last=False)
    return embedding


async def close_embedding_service() -> None:
    """关闭进程级 Embedding HTTP 客户端。"""
    service = _embedding_service
    if isinstance(service, LocalEmbeddingService):
        await service.close()


# 检索文本长度上限：BGE 模型只取前 512 tokens（约 500~2000 字符），
# 超长文本（几十万字符的长篇文档）全部发送只会拖慢服务端 tokenize
# 直至超时，对向量没有贡献。8000 为保守值，兼容将来更长上下文的模型。
MAX_EMBEDDING_TEXT_CHARS = 8000


def prepare_skill_text(skill: dict[str, Any] | Any) -> str:
    """
    组合 skill 的 name、description、content 生成用于 embedding 的文本，
    超过 MAX_EMBEDDING_TEXT_CHARS 的部分截断（模型侧本就不读取）。
    """
    parts = []
    if hasattr(skill, "name"):
        parts.append(skill.name)
        if skill.description:
            parts.append(skill.description)
        if skill.content:
            parts.append(skill.content)
    else:
        if skill.get("name"):
            parts.append(skill["name"])
        if skill.get("description"):
            parts.append(skill["description"])
        if skill.get("content"):
            parts.append(skill["content"])
    return " ".join(parts)[:MAX_EMBEDDING_TEXT_CHARS]


_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _contains_chinese(text: str) -> bool:
    """判断文本是否包含中文字符。"""
    return bool(_CJK_RE.search(text))


def prepare_search_query(query: str) -> str:
    """为 BGE 检索查询添加 query-side instruction，按查询语言选择指令。"""
    q = query.strip()
    instruction = BGE_QUERY_INSTRUCTION if _contains_chinese(q) else BGE_QUERY_INSTRUCTION_EN
    return f"{instruction}{q}"


async def generate_skill_embedding(skill: dict[str, Any] | Any) -> list[float] | None:
    """
    为单个 skill 生成 embedding
    """
    try:
        text = prepare_skill_text(skill)
        if not text.strip():
            return None
        embeddings = await generate_embeddings([text])
        return embeddings[0] if embeddings else None
    except Exception:
        return None
