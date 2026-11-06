from collections import defaultdict
from datetime import datetime, timedelta, timezone
import re

from sqlalchemy import case, desc, func, literal_column, select, text, or_
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any

from src.core.categories import category_label
from src.models.orm import DownloadHistory, Skill


# Hybrid search ranks a stable, bounded candidate pool so total and pagination
# do not change as the caller moves between pages.
HYBRID_CANDIDATE_LIMIT = 200


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符，配合 escape='\\' 使用。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def name_match_level(name: str | None, query: str) -> int:
    """name 与查询词的匹配强度：3=完全一致，2=前缀匹配，1=包含，0=无关。

    用于把精确/前缀匹配的结果钉在最前，避免被下载量等排序稀释。
    """
    n = (name or "").strip().lower()
    q = (query or "").strip().lower()
    if not n or not q:
        return 0
    if n == q:
        return 3
    if n.startswith(q):
        return 2
    if q in n:
        return 1
    return 0


def reciprocal_rank_fusion(*ranked_lists: list[dict], k: int = 60) -> list[dict]:
    """
    合并多个排序列表，使用 RRF 算法
    """
    scores = defaultdict(float)
    item_map = {}

    for ranked_list in ranked_lists:
        for rank, item in enumerate(ranked_list):
            item_id = item.get("id") or item.get("skill_id")
            if item_id:
                scores[item_id] += 1 / (rank + k)
                item_map[item_id] = item

    return [
        item_map[item_id]
        for item_id, _ in sorted(scores.items(), key=lambda x: -x[1])
    ]


class SearchService:
    def __init__(self, session: AsyncSession):
        self.session = session

    def _apply_skill_filters(
        self,
        query,
        *,
        category: list[str] | None = None,
        platform: str | None = None,
        tags: list[str] | None = None,
        security_level: list[str] | None = None,
    ):
        # 全局规则：排除未检测（risk_score 为空）的 Skill，不进入搜索
        query = query.where(Skill.risk_score.is_not(None))
        if category:
            conditions = []
            normal_cats = []
            for c in category:
                if c.lower() == "others":
                    conditions.append(
                        or_(
                            Skill.category.is_(None),
                            Skill.category == "",
                            func.lower(Skill.category).in_(["others", "other"]),
                        )
                    )
                else:
                    normal_cats.append(c)
            if normal_cats:
                conditions.append(Skill.category.in_(normal_cats))
            if conditions:
                query = query.where(or_(*conditions))
        if platform:
            platforms = [p for p in platform.split(",") if p] if isinstance(platform, str) else platform
            query = query.where(Skill.platform.in_(platforms))
        if tags:
            query = query.where(Skill.tags.contains(tags))
        if security_level:
            conditions = []
            for level in security_level:
                if level == "安全":
                    conditions.append(Skill.risk_score <= 20)
                elif level == "低风险":
                    conditions.append(Skill.risk_score.between(21, 50))
                elif level == "中风险":
                    conditions.append(Skill.risk_score.between(51, 80))
                elif level == "高风险":
                    conditions.append(Skill.risk_score >= 81)
            if conditions:
                query = query.where(or_(*conditions))
        return query

    def _item_version_sort_key(self, item: dict[str, Any]) -> tuple[int, tuple[int, ...], int, str, str, str]:
        version = str(item.get("version") or "").strip()
        if version.lower() == "latest":
            return (
                2,
                tuple(),
                0,
                "",
                str(item.get("updated_at") or ""),
                str(item.get("created_at") or ""),
            )
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
                str(item.get("updated_at") or ""),
                str(item.get("created_at") or ""),
            )
        return (
            0,
            tuple(),
            0,
            "",
            str(item.get("updated_at") or ""),
            str(item.get("created_at") or ""),
        )

    @staticmethod
    def _dedupe_key(item: dict[str, Any]) -> str:
        """去重键：同名 + 同描述视为同一 skill（跨仓库镜像副本），合并为一条。

        相比按 skill_id 去重（skills 表 skill_id 唯一，实为空操作），本口径
        能合并镜像副本（如 detect 在 launchdarkly 的 agent-skills/ai-tooling/
        experimental-agent-skills 各一份），同时保留同名不同描述的不同 skill
        （如多个 skill-creator 各有各的描述）。
        """
        name = str(item.get("name") or "").strip().lower()
        description = str(item.get("description") or "").strip()
        if name and description:
            return f"{name}\x00{description}"
        # name/description 缺失时回退到 skill_id，避免误合并无关项
        return str(item.get("skill_id") or "").strip().lower()

    @staticmethod
    def _truncate_by_relative_gap(
        results: list[dict[str, Any]],
        gap_threshold: float = 0.01,
        min_keep: int = 3,
        max_keep: int = 15,
    ) -> list[dict[str, Any]]:
        """相对差距截断：在相似度出现明显断崖处切掉噪声尾。

        从第 min_keep 条之后开始，遇到与上一条相似度差距 >= gap_threshold 的
        断崖即截断；若始终无断崖（向量距离平滑衰减、噪声与相关项难以区分），
        则退回 max_keep 条数上限，避免返回大量低相关结果。

        阈值取值依据：目标与「相关项」差距大（~0.14），但「相关项」与「噪声」
        差距小（~0.01-0.02），故只能用小阈值切掉紧贴的噪声尾，同时靠 min_keep
        兜底不误伤目标、max_keep 兜底限制无断崖场景的结果数。
        """
        if len(results) <= min_keep:
            return results
        keep = min_keep
        for i in range(min_keep, len(results)):
            if keep >= max_keep:
                break
            prev_sim = results[i - 1].get("similarity")
            cur_sim = results[i].get("similarity")
            if prev_sim is None or cur_sim is None:
                keep = i + 1
                continue
            if prev_sim - cur_sim >= gap_threshold:
                break
            keep = i + 1
        return results[:keep]

    def _dedupe_skill_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}

        for index, item in enumerate(results):
            dedupe_key = self._dedupe_key(item)
            if not dedupe_key:
                continue

            existing = grouped.get(dedupe_key)
            if existing is None:
                grouped[dedupe_key] = {
                    "first_index": index,
                    "representative": item,
                }
                continue

            existing_item = existing["representative"]
            if self._item_version_sort_key(item) > self._item_version_sort_key(existing_item):
                existing["representative"] = item

        ordered = sorted(grouped.values(), key=lambda entry: entry["first_index"])
        return [entry["representative"] for entry in ordered]

    async def search_skills(
        self,
        query: str,
        limit: int = 20,
        offset: int = 0,
        category: list[str] | None = None,
        platform: str | None = None,
        tags: list[str] | None = None,
        security_level: list[str] | None = None,
        embedding: list[float] | None = None,
        mode: str = "hybrid",
        scope: str = "summary",
        translated_query: str | None = None,
        translated_embedding: list[float] | None = None,
        sort_by: str = "relevance",
        sort_period: str | None = None,
    ) -> dict[str, Any]:
        # 显式排序（热门/最新）时先取无分页候选集，再统一排序分页；
        # 默认 relevance 保持各模式现有相关度排序与分页行为不变。
        has_sort = sort_by in ("updated_at", "download_count")

        if embedding and mode == "semantic":
            vector_results = await self._vector_search(
                embedding=embedding,
                # 去重前多取候选：镜像副本可能很多（如同一 skill 在组织多个仓库
                # 各有一份），只取 limit 条去重后会不足一页
                limit=HYBRID_CANDIDATE_LIMIT,
                offset=0,
                category=category,
                platform=platform,
                tags=tags,
                security_level=security_level,
            )
            # 语义路径同样去重镜像副本 + 精确匹配置顶（此前直接 return 会漏掉）
            deduped = self._dedupe_skill_results(vector_results.get("results", []))
            deduped = self._pin_exact_matches(deduped, query)
            # 相对差距截断去噪：在相似度断崖处切掉噪声尾（保留最少 3 条兜底）
            deduped = self._truncate_by_relative_gap(deduped)
            if not has_sort:
                return {
                    **vector_results,
                    "results": deduped[offset:offset + limit],
                    "total": len(deduped),
                }
            return await self._sorted_search_response(
                deduped,
                query=query, mode="semantic", offset=offset, limit=limit,
                sort_by=sort_by, sort_period=sort_period,
            )

        is_hybrid = bool(embedding and mode == "hybrid")
        text_results = await self._text_search(
            query=query,
            limit=HYBRID_CANDIDATE_LIMIT if (is_hybrid or has_sort) else limit,
            offset=0 if (is_hybrid or has_sort) else offset,
            category=category,
            platform=platform,
            tags=tags,
            scope=scope,
            security_level=security_level,
        )

        if is_hybrid:
            vector_results = await self._vector_search(
                embedding=embedding,
                limit=HYBRID_CANDIDATE_LIMIT,
                offset=0,
                category=category,
                platform=platform,
                tags=tags,
                security_level=security_level,
            )
            # 融合前截断向量腿：此时仍按距离单调排序，截断才有意义。
            # RRF 融合后 similarity 已乱序，对融合结果做差距截断无效。
            vector_list = self._truncate_by_relative_gap(
                vector_results.get("results", []),
            )
            ranked_lists = [text_results.get("results", []), vector_list]
            if translated_query:
                translated_text = await self._text_search(
                    query=translated_query, limit=HYBRID_CANDIDATE_LIMIT,
                    category=category, platform=platform, tags=tags,
                    scope=scope, security_level=security_level,
                )
                ranked_lists.append(translated_text.get("results", []))
            if translated_embedding:
                translated_vector = await self._vector_search(
                    embedding=translated_embedding, limit=HYBRID_CANDIDATE_LIMIT,
                    offset=0, category=category, platform=platform, tags=tags,
                    security_level=security_level,
                )
                ranked_lists.append(self._truncate_by_relative_gap(
                    translated_vector.get("results", []),
                ))
            combined = reciprocal_rank_fusion(*ranked_lists)
            deduped = self._dedupe_skill_results(combined)
            # A：先对完整候选集置顶精确匹配，再分页（否则翻页无效）
            deduped = self._pin_exact_matches(deduped, query)
            if has_sort:
                return await self._sorted_search_response(
                    deduped,
                    query=query, mode="hybrid", offset=offset, limit=limit,
                    sort_by=sort_by, sort_period=sort_period,
                )
            return {
                "results": deduped[offset:offset + limit],
                "total": len(deduped),
                "query": query,
                "skip": offset,
                "limit": limit,
                "mode": "hybrid",
            }
        elif has_sort:
            # 纯文本模式 + 显式排序：候选集为无分页取回的相关度 Top N
            deduped = self._dedupe_skill_results(text_results["results"])
            return await self._sorted_search_response(
                deduped,
                query=query, mode="text", offset=offset, limit=limit,
                sort_by=sort_by, sort_period=sort_period,
            )
        else:
            # _text_search 已在 SQL 层完成 limit/offset 分页（见上方 limit/offset 传参），
            # 这里不能再按 offset 二次切片，否则第 2 页（offset>0）会得到空列表。
            deduped = self._dedupe_skill_results(text_results["results"])
            return {
                "results": deduped,
                "total": text_results["total"],
                "query": query,
                "skip": offset,
                "limit": limit,
                "mode": "text",
            }

    @staticmethod
    def _pin_exact_matches(
        results: list[dict[str, Any]], query: str
    ) -> list[dict[str, Any]]:
        """A：name 完全一致/前缀匹配的结果强制置顶，且相对顺序保持不变。

        与 sort_by 无关——即便按下载量/时间排序，精确命中的 skill 也钉在最前，
        避免 0 下载的新 skill 被排到翻页之后。
        """
        if not query.strip():
            return results
        exact: list[dict[str, Any]] = []
        prefix: list[dict[str, Any]] = []
        rest: list[dict[str, Any]] = []
        for item in results:
            level = name_match_level(item.get("name"), query)
            if level == 3:
                exact.append(item)
            elif level == 2:
                prefix.append(item)
            else:
                rest.append(item)
        return exact + prefix + rest

    async def _sorted_search_response(
        self,
        candidates: list[dict[str, Any]],
        *,
        query: str,
        mode: str,
        offset: int,
        limit: int,
        sort_by: str,
        sort_period: str | None,
    ) -> dict[str, Any]:
        """在搜索候选集内应用显式排序，语义与列表端点一致。

        - 热门-本周/本月：inner join download_history 周期聚合，仅保留周期内
          有下载的结果并按周期下载量排序（total 同口径）；结果附 period_downloads。
        - 其他排序：直接按候选字段排序，total 为候选总数。
        """
        if sort_period in ("week", "month"):
            ordered, total = await self._order_candidates_by_period(candidates, sort_period)
        else:
            ordered = self._sort_candidates(candidates, sort_by)
            total = len(ordered)
        # A：显式排序后再次置顶精确匹配，避免排序把精确命中挤到翻页之后
        ordered = self._pin_exact_matches(ordered, query)
        return {
            "results": ordered[offset:offset + limit],
            "total": total,
            "query": query,
            "skip": offset,
            "limit": limit,
            "mode": mode,
        }

    @staticmethod
    def _sort_candidates(
        candidates: list[dict[str, Any]], sort_by: str
    ) -> list[dict[str, Any]]:
        if sort_by == "download_count":
            return sorted(
                candidates,
                key=lambda c: (
                    c.get("download_count") or 0,
                    c.get("updated_at") or "",
                    c.get("created_at") or "",
                ),
                reverse=True,
            )
        return sorted(
            candidates,
            key=lambda c: (c.get("updated_at") or "", c.get("created_at") or ""),
            reverse=True,
        )

    async def _order_candidates_by_period(
        self, candidates: list[dict[str, Any]], sort_period: str
    ) -> tuple[list[dict[str, Any]], int]:
        # cutoff 口径与 SkillRepository.list 一致（UTC 周一 00:00 / 月初 00:00）
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
        id_list = [c["id"] for c in candidates]
        if not id_list:
            return [], 0

        rows = (
            await self.session.execute(
                select(Skill.id, dl_subquery.c.period_downloads)
                .join(dl_subquery, Skill.id == dl_subquery.c.resource_id)
                .where(Skill.id.in_(id_list))
                .order_by(
                    desc(dl_subquery.c.period_downloads),
                    desc(Skill.updated_at),
                    desc(Skill.created_at),
                )
            )
        ).all()

        by_id = {c["id"]: c for c in candidates}
        ordered: list[dict[str, Any]] = []
        for skill_id, period_downloads in rows:
            item = dict(by_id.get(str(skill_id), {}))
            if not item:
                continue
            item["period_downloads"] = int(period_downloads or 0)
            ordered.append(item)
        return ordered, len(ordered)

    async def _text_search(
        self,
        query: str,
        limit: int = 20,
        offset: int = 0,
        category: list[str] | None = None,
        platform: str | None = None,
        tags: list[str] | None = None,
        scope: str = "summary",
        security_level: list[str] | None = None,
    ) -> dict[str, Any]:
        # Keep these expressions in sync with migrations 009 and 010.
        # String concatenation (||) is immutable in PostgreSQL and can be indexed,
        # unlike concat(), which is only STABLE.
        search_text = (
            func.coalesce(Skill.name, "") + " "
            + func.coalesce(Skill.description, "")
        )
        if scope == "full":
            search_text = search_text + " " + func.coalesce(Skill.content, "")
        search_config = literal_column("'zhcfg'::regconfig")

        ts_query = func.plainto_tsquery(search_config, query)
        # C：name 权重提升。name 单独的 ts_rank 权重高于 name+description 合并
        # 文本，避免 description 里的偶然词汇（如 "skill"）稀释 name 的命中。
        # 注意保持函数 IMMUTABLE（migration 009/010 的 GIN 索引基于此表达式）。
        name_text = func.coalesce(Skill.name, "")
        rank_expression = (
            func.ts_rank(
                func.to_tsvector(search_config, name_text),
                ts_query,
            ) * 3.0
            + func.ts_rank(
                func.to_tsvector(search_config, search_text),
                ts_query,
            )
        )
        # name 里的字面量命中（如连字符整体命中）额外加权：
        # plainto_tsquery 会把 "spack-develop-skill" 拆成多个 token，
        # 单靠 ts_rank 会被所有含 "skill" 的结果稀释。
        escaped_query = _escape_like(query)
        name_literal_rank = case(
            (func.lower(name_text) == query.lower(), 3.0),
            (
                func.lower(name_text).like(f"{escaped_query.lower()}%", escape="\\"),
                2.0,
            ),
            (
                func.lower(name_text).like(f"%{escaped_query.lower()}%", escape="\\"),
                1.0,
            ),
            else_=0.0,
        )
        rank_expression = rank_expression + name_literal_rank * 10.0

        search_predicate = (
            (func.to_tsvector(search_config, search_text).op("@@")(ts_query)) |
            (Skill.name.ilike(f"%{query}%")) |
            (Skill.description.ilike(f"%{query}%"))
        )
        # Select only fields returned by the API. Loading content, metadata,
        # and the 768-dimensional embedding for every candidate makes common
        # queries such as "code" unnecessarily expensive.
        base_query = select(
            Skill.id,
            Skill.skill_id,
            Skill.name,
            Skill.description,
            Skill.version,
            Skill.author,
            Skill.source,
            Skill.source_url,
            Skill.category,
            Skill.tags,
            Skill.platform,
            Skill.risk_score,
            Skill.download_count,
            Skill.rating,
            Skill.created_at,
            Skill.updated_at,
            rank_expression.label("rank"),
        ).where(search_predicate)

        base_query = self._apply_skill_filters(
            base_query,
            category=category,
            platform=platform,
            tags=tags,
            security_level=security_level,
        )

        # skills.skill_id is unique; DISTINCT adds sorting/hash work without
        # changing the result.
        count_query = select(func.count(Skill.id)).where(
            search_predicate
        )
        count_query = self._apply_skill_filters(
            count_query,
            category=category,
            platform=platform,
            tags=tags,
            security_level=security_level,
        )
        total_result = await self.session.execute(count_query)
        total = total_result.scalar() or 0

        # skills.skill_id is unique, so fetch only the requested page. The old
        # 10x candidate overfetch loaded up to 240 rows for a 12-card page.
        candidate_query = (
            base_query
            .order_by(rank_expression.desc(), Skill.download_count.desc(), desc(Skill.updated_at), desc(Skill.created_at))
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(candidate_query)
        rows = result.mappings().all()

        results = []
        for row in rows:
            results.append({
                "id": str(row["id"]),
                "skill_id": row["skill_id"],
                "name": row["name"],
                "description": row["description"],
                "version": row["version"],
                "author": row["author"],
                "source": row["source"],
                "source_url": row["source_url"],
                "category": row["category"],
                "category_label": category_label(row["category"]),
                "tags": row["tags"] or [],
                "platform": row["platform"],
                "risk_score": row["risk_score"],
                "download_count": row["download_count"],
                "rating": row["rating"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                "text_rank": float(row["rank"]) if row["rank"] else 0,
            })

        return {"results": results, "total": total}

    async def _vector_search(
        self,
        embedding: list[float],
        limit: int = 20,
        offset: int = 0,
        category: list[str] | None = None,
        platform: str | None = None,
        tags: list[str] | None = None,
        security_level: list[str] | None = None,
        # 兜底地板阈值：只排除完全不相关的垃圾结果，真正的去噪由
        # _truncate_by_relative_gap 的相对差距截断负责。
        # 注意随相似度公式联动：余弦值 cos=1-d²/2 整体低于旧的 1/(1+d)，
        # 旧 0.47（距离≤1.128）换算成余弦约 0.36，故地板下移到 0.35。
        min_similarity: float = 0.35,
    ) -> dict[str, Any]:
        embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

        where_clauses = ["embedding IS NOT NULL", "risk_score IS NOT NULL"]
        params: dict[str, Any] = {"limit": limit, "offset": offset, "embedding": embedding_str}

        if category:
            placeholders = []
            for idx, c in enumerate(category):
                key = f"cat_{idx}"
                placeholders.append(f":{key}")
                params[key] = c
            where_clauses.append(f"category IN ({', '.join(placeholders)})")
        if platform:
            platforms = [p for p in platform.split(",") if p] if isinstance(platform, str) else [platform]
            placeholders = []
            for idx, p in enumerate(platforms):
                key = f"plat_{idx}"
                placeholders.append(f":{key}")
                params[key] = p
            where_clauses.append(f"platform IN ({', '.join(placeholders)})")
        if tags:
            where_clauses.append("tags @> :tags")
            params["tags"] = tags
        if security_level:
            sl_conditions = []
            for idx, level in enumerate(security_level):
                if level == "安全":
                    sl_conditions.append(f"risk_score <= :sl_{idx}")
                    params[f"sl_{idx}"] = 20
                elif level == "低风险":
                    sl_conditions.append(f"(risk_score >= :sl_low_{idx} AND risk_score <= :sl_high_{idx})")
                    params[f"sl_low_{idx}"] = 21
                    params[f"sl_high_{idx}"] = 50
                elif level == "中风险":
                    sl_conditions.append(f"(risk_score >= :sl_low_{idx} AND risk_score <= :sl_high_{idx})")
                    params[f"sl_low_{idx}"] = 51
                    params[f"sl_high_{idx}"] = 80
                elif level == "高风险":
                    sl_conditions.append(f"risk_score >= :sl_{idx}")
                    params[f"sl_{idx}"] = 81
            if sl_conditions:
                where_clauses.append("(" + " OR ".join(sl_conditions) + ")")

        where_sql = " AND ".join(where_clauses)

        sql = text(f"""
            SELECT id, skill_id, name, description, version, author, source,
                   source_url, category, tags, platform,
                   risk_score, download_count, rating, created_at, updated_at,
                   embedding <-> CAST(:embedding AS vector) AS distance
            FROM skills
            WHERE {where_sql}
            ORDER BY distance ASC
            LIMIT :limit OFFSET :offset
        """)

        result = await self.session.execute(sql, params)
        rows = result.fetchall()

        results = []
        for row in rows:
            distance = float(row.distance)
            # 向量已归一化（norm=1），L2 距离 d 与余弦相似度精确换算 cos=1-d²/2。
            # 相比旧的 1/(1+d)，余弦值区分度更高（强匹配 0.66 vs 噪声 0.50），
            # 让「目标 vs 噪声」的断崖更明显，便于相对差距截断。
            similarity = max(-1.0, min(1.0, 1.0 - (distance * distance) / 2.0))
            if similarity < min_similarity:
                continue
            results.append({
                "id": str(row.id),
                "skill_id": row.skill_id,
                "name": row.name,
                "description": row.description,
                "version": row.version,
                "author": row.author,
                "source": row.source,
                "source_url": row.source_url,
                "category": row.category,
                "category_label": category_label(row.category),
                "tags": row.tags or [],
                "platform": row.platform,
                "risk_score": row.risk_score,
                "download_count": row.download_count,
                "rating": row.rating,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "distance": distance,
                "similarity": similarity,
            })

        return {"results": results, "total": len(results), "mode": "semantic"}

    async def search_agents(
        self,
        query: str,
        limit: int = 20,
        offset: int = 0,
        category: str | None = None,
    ) -> dict[str, Any]:
        from src.models.orm import Agent

        agent_search_text = func.concat(
            Agent.name, " ",
            func.coalesce(Agent.description, "")
        )

        search_query = select(
            Agent,
            func.ts_rank(
                func.to_tsvector("zhcfg", agent_search_text),
                func.plainto_tsquery("zhcfg", query)
            ).label("rank")
        )

        ts_query = func.plainto_tsquery("zhcfg", query)
        search_query = search_query.where(
            func.to_tsvector("zhcfg", agent_search_text).op("@@")(ts_query)
        )

        if category:
            search_query = search_query.where(Agent.category == category)

        search_query = search_query.order_by(text("rank desc"), Agent.download_count.desc())
        search_query = search_query.offset(offset).limit(limit)

        result = await self.session.execute(search_query)
        rows = result.all()

        results = []
        for agent, rank in rows:
            results.append({
                "id": str(agent.id),
                "agent_id": agent.agent_id,
                "name": agent.name,
                "description": agent.description,
                "author": agent.author,
                "source": agent.source,
                "source_url": agent.source_url,
                "category": agent.category,
                "tags": agent.tags or [],
                "platform": agent.platform,
                "risk_score": agent.risk_score,
                "download_count": agent.download_count,
                "rating": agent.rating,
                "created_at": agent.created_at.isoformat() if agent.created_at else None,
                "rank": float(rank) if rank else 0,
            })

        return {
            "results": results,
            "total": len(results),
            "query": query,
            "skip": offset,
            "limit": limit,
        }


_search_service: SearchService | None = None


def get_search_service(session: AsyncSession) -> SearchService:
    return SearchService(session)
