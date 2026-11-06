from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx
import yaml

from src.ai.llm import chat_completion_sync
from src.core.config import get_settings

_logger = logging.getLogger(__name__)
settings = get_settings()


def load_skill_categories(repository_path: Path | None = None) -> list[str]:
    """Read category names from ``templates/skill-spec.yaml`` in the catalog."""
    if repository_path is None:
        # catalog 未给定时惰性同步 openEuler-skills 仓库
        # （正常路径由扫描器传入 repository_path）
        from src.skills.config import sync_openEuler_skills_repo

        repository_path = sync_openEuler_skills_repo()
    spec_path = repository_path.expanduser() / "templates" / "skill-spec.yaml"
    if not spec_path.exists():
        raise FileNotFoundError(f"Skill spec not found: {spec_path}")
    with spec_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    categories = data.get("categories", []) if isinstance(data, dict) else []
    return [str(item).strip() for item in categories if str(item).strip()] if isinstance(categories, list) else []


# 网络抖动（读超时/连接重置/5xx/429）的重试次数与间隔；重试耗尽后降级
# 沿用 frontmatter category 而不是把整仓扫描打挂
CATEGORY_RETRY_ATTEMPTS = 2
CATEGORY_RETRY_BACKOFF_SECONDS = 2.0


class CategoryClassificationError(RuntimeError):
    pass


class DeepSeekCategoryClassifier:
    def __init__(self, repository_path: Path | None = None):
        self.repository_path = repository_path
        self.categories = self._load_categories()
        self.model_name = self._load_model_name()
        self.api_key = self._load_api_key()
        self.base_url = self._load_base_url()
        self.timeout = float(self._load_timeout())

    def classify(
        self,
        *,
        skill_file: Path,
        metadata: dict[str, object],
        content: str,
        source_url: str,
    ) -> str | None:
        metadata_category = self._normalize_category(metadata.get("category"))
        if metadata_category is not None:
            return metadata_category

        if not self.api_key or not self.categories:
            return None

        prompt = self._build_prompt(
            skill_file=skill_file,
            metadata=metadata,
            content=content,
            source_url=source_url,
        )

        declared_category = str(metadata.get("category") or "").strip() or None
        for attempt in range(1, CATEGORY_RETRY_ATTEMPTS + 1):
            try:
                response_text = self._call_deepseek(prompt)
                category = self._normalize_category(response_text)
                if category is not None:
                    _logger.info(
                        "DeepSeek category classification success for %s: %s",
                        skill_file, category,
                    )
                else:
                    _logger.warning(
                        "DeepSeek category classification returned unrecognized "
                        "value for %s: %r",
                        skill_file, response_text,
                    )
                return category
            except Exception as exc:
                if not self._is_transient_error(exc):
                    message = f"DeepSeek category classification failed for {skill_file}: {exc}"
                    _logger.warning(message)
                    raise CategoryClassificationError(message) from exc
                if attempt < CATEGORY_RETRY_ATTEMPTS:
                    _logger.warning(
                        "DeepSeek category classification transient failure for %s "
                        "(attempt %d/%d): %s",
                        skill_file, attempt, CATEGORY_RETRY_ATTEMPTS, exc,
                    )
                    time.sleep(CATEGORY_RETRY_BACKOFF_SECONDS)
                    continue
                # 重试耗尽的网络故障降级：frontmatter category 合法（在类别表
                # 内，经 _normalize_category 归一）则沿用；非法或缺失返回 None，
                # 落库为空、下轮扫描重新分类，避免非法值被缓存固化。
                # 网络抖动是常态，为一个 skill 的分类把整仓扫描回滚代价过高；
                # 真正的配置/凭据类错误仍走 CategoryClassificationError 保
                # 持 fatal 语义。
                fallback = self._normalize_category(declared_category)
                if fallback is None and declared_category is not None:
                    _logger.warning(
                        "DeepSeek category classification degraded: frontmatter "
                        "category %r of %s is not a valid category, leave empty "
                        "for reclassification: %s",
                        declared_category, skill_file, exc,
                    )
                else:
                    _logger.warning(
                        "DeepSeek category classification degraded to frontmatter "
                        "category for %s: %s",
                        skill_file, exc,
                    )
                return fallback
        return None

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        """网络/服务端瞬时故障（可重试、可降级），区别于配置类致命错误。"""
        if isinstance(exc, httpx.TransportError):
            # 含 ReadTimeout / ConnectError 等全部传输层错误
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            return status == 429 or status >= 500
        return False

    def _load_categories(self) -> list[str]:
        return load_skill_categories(self.repository_path)

    def _load_model_name(self) -> str:
        return settings.model.name.strip()

    def _load_api_key(self) -> str:
        return settings.model.api_key.strip()

    def _load_base_url(self) -> str:
        return settings.model.base_url.rstrip("/")

    def _load_timeout(self) -> float:
        return float(settings.model.timeout)

    def _build_prompt(
        self,
        *,
        skill_file: Path,
        metadata: dict[str, object],
        content: str,
        source_url: str,
    ) -> str:
        description = str(metadata.get("description") or "").strip()
        tags = metadata.get("tags") or []
        if isinstance(tags, list):
            tag_text = ", ".join(str(tag) for tag in tags[:10])
        else:
            tag_text = str(tags)
        excerpt = content.strip()[:2000]
        categories_text = ", ".join(self.categories)

        return (
            "You are classifying an AI skill into exactly one category.\n"
            f"Available categories: {categories_text}\n"
            "Return only one category from the list above. Do not explain.\n"
            "If you are uncertain, return others.\n\n"
            f"Skill file: {skill_file.name}\n"
            f"Source URL: {source_url}\n"
            f"Name: {metadata.get('name') or skill_file.parent.name}\n"
            f"Description: {description}\n"
            f"Tags: {tag_text}\n"
            "Content excerpt:\n"
            f"{excerpt}"
        )

    def _call_deepseek(self, prompt: str) -> str:
        # 统一走 src.ai.llm：请求体、enable_thinking、鉴权、响应解析、耗时
        # 日志都收敛在一处，与搜索翻译共享同一口径（此前两处各写一遍，
        # 翻译漏掉关闭思考被 Qwen3 长思维链拖慢到 30-45s）。
        messages = [
            {
                "role": "system",
                "content": "You classify skills into one predefined category.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ]
        return chat_completion_sync(
            messages,
            temperature=0,
            timeout=self.timeout,
        )

    def _normalize_category(self, value: object) -> str | None:
        if value is None:
            return None

        text = str(value).strip()
        if not text:
            return None

        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                text = str(parsed.get("category") or "").strip()
        except Exception:
            pass

        lowered = text.lower()
        for category in self.categories:
            if lowered == category.lower():
                return category
        return None
