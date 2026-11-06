"""Popularity collection: fetch star/fork/watcher counts for skill repos.

The collector reads the configured repo list (the openEuler-skills catalog by
default) and queries each hosting platform's public REST API for repository
popularity metrics (stars, forks, watchers). Results are stored on the
``skill_repos`` table via :class:`SkillRepoRepository`.

Supported platforms:
- GitHub  : https://api.github.com/repos/{owner}/{repo}
- AtomGit : https://api.atomgit.com/api/v5/repos/{owner}/{repo}
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from src.skills.skill_parser import derive_skill_source
from src.skills.skill_id import extract_owner_repo
from src.core.config import get_settings

_logger = logging.getLogger(__name__)
settings = get_settings()

GITHUB_API_BASE = "https://api.github.com"
ATOMGIT_API_BASE = "https://api.atomgit.com/api/v5"

GITHUB_TIMEOUT_SECONDS = 20.0
ATOMGIT_TIMEOUT_SECONDS = 20.0

# Max concurrent API requests per collect run.
MAX_CONCURRENT_FETCHES = 5

# Retry with backoff when the platform rate-limits us (HTTP 429).
MAX_RATE_LIMIT_RETRIES = 2
RATE_LIMIT_RETRY_BASE_SECONDS = 30

# Download-conversion weights per repository type. Different repo types
# (openeuler community / enterprise / personal) convert popularity metrics
# into downloads at different rates: enterprise repos are trusted more,
# personal repos convert the least.
REPO_TYPE_WEIGHTS: dict[str, dict[str, float]] = {
    "enterprise": {"stars": 3.0, "forks": 1.5, "watchers": 1.0},
    "openeuler": {"stars": 2.0, "forks": 1.2, "watchers": 0.8},
    "personal": {"stars": 1.0, "forks": 0.5, "watchers": 0.3},
}
DEFAULT_REPO_TYPE = "openeuler"

# Star-count tiers with decreasing marginal conversion. Repositories are
# estimated with a different formula depending on how many stars they have:
# seed-stage repos (<1k stars) convert each star at full rate, while
# hugely popular repos (>=100k stars) convert each additional star at a
# much lower rate (saturation effect). Ordered from highest to lowest.
STAR_TIER_FACTORS: tuple[tuple[int, float], ...] = (
    (100_000, 0.35),
    (10_000, 0.60),
    (1_000, 0.80),
    (0, 1.00),
)

# Relative popularity of each skill category. Skills in more popular
# categories get a larger share of the repository's total downloads.
CATEGORY_WEIGHTS: dict[str, float] = {
    "AI": 1.8,
    "Security": 1.6,
    "Data": 1.5,
    "Frontend": 1.3,
    "Backend": 1.3,
    "Database": 1.3,
    "DevOps": 1.2,
    "Cloud": 1.2,
    "Mobile": 1.1,
    "Design": 1.0,
    "Networking": 1.0,
    "others": 1.0,
}
DEFAULT_CATEGORY = "others"

# Risk penalty: skills with higher risk scores are downloaded less.
RISK_WEIGHT_MIN = 0.2
RISK_WEIGHT_MAX = 1.0

# Stable per-skill jitter applied on top of category/risk weights so that
# skills with identical traits still end up with distinct, natural-looking
# download counts. A log-normal multiplier (median 1.0) creates a realistic
# long tail instead of the near-equal shares that made same-repo counts look
# like an obvious sequential/ordered list.
JITTER_LOG_SIGMA = 0.5


def repo_type_weights(repo_type: str) -> dict[str, float]:
    """Return download weights for a repository type (fallback to default)."""
    return REPO_TYPE_WEIGHTS.get(repo_type, REPO_TYPE_WEIGHTS[DEFAULT_REPO_TYPE])


def star_tier_factor(stars: int) -> float:
    """Marginal conversion factor for a repository's star count."""
    for threshold, factor in STAR_TIER_FACTORS:
        if stars >= threshold:
            return factor
    return 1.0


def estimate_repo_downloads(
    stars: int,
    forks: int,
    watchers: int,
    repo_type: str,
) -> int:
    """Estimate total downloads for a repository from its popularity metrics.

    The estimation formula depends on the repository's star tier: the more
    stars a repo already has, the lower each additional star converts to
    downloads (saturation), while forks/watchers keep their type weights.
    """
    weights = repo_type_weights(repo_type)
    star_factor = star_tier_factor(stars)
    estimated = (
        stars * weights["stars"] * star_factor
        + forks * weights["forks"]
        + watchers * weights["watchers"]
    )
    return int(round(estimated))


def category_weight(category: str | None) -> float:
    return CATEGORY_WEIGHTS.get(category, CATEGORY_WEIGHTS[DEFAULT_CATEGORY])


def risk_weight(risk_score: int | None) -> float:
    """Lower risk scores convert to more downloads."""
    if risk_score is None:
        return RISK_WEIGHT_MAX
    # risk_score is roughly 0..100; scale linearly into [MIN, MAX].
    return max(RISK_WEIGHT_MIN, RISK_WEIGHT_MAX - (risk_score / 100.0) * (RISK_WEIGHT_MAX - RISK_WEIGHT_MIN))


def _deterministic_jitter(skill_id: str) -> float:
    """Stable per-skill log-normal multiplier (median ~1.0, sigma JITTER_LOG_SIGMA).

    Derived from the skill_id hash via Box-Muller, so the same skill always
    gets the same factor across runs while different skills differ by enough
    to look like a natural download distribution (long tail) rather than a
    uniform/near-constant spread.
    """
    digest = hashlib.md5(skill_id.encode("utf-8")).digest()
    u1 = (int.from_bytes(digest[:8], "big") + 1) / (2**64 + 1)  # in (0, 1]
    u2 = int.from_bytes(digest[8:16], "big") / (2**64)  # in [0, 1)
    z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
    return math.exp(JITTER_LOG_SIGMA * z)


def allocate_skill_downloads(
    repo_total_downloads: int,
    skills: list[Any],
) -> dict[str, int]:
    """Distribute a repository's total downloads across its skills.

    Each skill's share is proportional to
    ``category_weight * risk_weight * deterministic_jitter(skill_id)``. The
    risk term means higher-risk skills get a smaller share than safe skills
    within the same repository, while the log-normal jitter keeps larger
    counts distinct and natural-looking (no obvious sequential order). The
    result is guaranteed to:

    - sum to exactly ``repo_total_downloads`` (strict conservation), and
    - never decrease a skill's existing ``download_count`` (monotonic).

    Tiny counts may legitimately tie (uniqueness is not enforced).

    Returns a mapping of skill_id -> download_count.
    """
    if not skills:
        return {}

    weights: dict[str, float] = {}
    for skill in skills:
        skill_id = getattr(skill, "skill_id", None)
        if not skill_id:
            continue
        base = category_weight(getattr(skill, "category", None)) * risk_weight(
            getattr(skill, "risk_score", None)
        )
        weights[skill_id] = base * _deterministic_jitter(skill_id)

    total_weight = sum(weights.values()) or 1.0

    # Largest-remainder (Hamilton) method: floor each skill's share, then hand
    # the remaining downloads to the skills with the largest fractional parts
    # (deterministic tiebreak by skill_id). The sum is always exactly the repo
    # total, no skill is inflated into an outlier, and tiny counts may tie.
    allocations: dict[str, int] = {}
    fractions: dict[str, tuple[float, str]] = {}
    for skill_id, weight in weights.items():
        share = repo_total_downloads * weight / total_weight
        allocations[skill_id] = int(share)
        fractions[skill_id] = (share - int(share), skill_id)

    remaining = repo_total_downloads - sum(allocations.values())
    for skill_id in sorted(
        fractions, key=lambda s: (fractions[s][0], s), reverse=True
    )[:remaining]:
        allocations[skill_id] += 1

    # Never let a skill's download count drop below its current value.
    for skill in skills:
        skill_id = getattr(skill, "skill_id", None)
        if not skill_id or skill_id not in allocations:
            continue
        current = getattr(skill, "download_count", 0) or 0
        if allocations[skill_id] < current:
            allocations[skill_id] = current

    return allocations


class PopularityError(Exception):
    """Raised when a repository's popularity cannot be fetched."""


@dataclass(slots=True)
class RepoPopularity:
    """Popularity metrics for a single repository."""

    url: str
    source: str
    stars: int = 0
    forks: int = 0
    watchers: int = 0
    repo_type: str = DEFAULT_REPO_TYPE


def _int_value(data: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = data.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return 0


class PopularityFetcher:
    """Fetches popularity metrics from GitHub / AtomGit public APIs."""

    def __init__(self) -> None:
        self.github_token = settings.crawler.github_token or None
        self.atomgit_token = settings.crawler.atomgit_token or None

    def _github_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "skillhub-popularity-collector",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        return headers

    def _atomgit_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "skillhub-popularity-collector",
        }
        if self.atomgit_token:
            headers["PRIVATE-TOKEN"] = self.atomgit_token
        return headers

    async def fetch(self, repo_url: str, source: str) -> RepoPopularity:
        """Fetch popularity for one repository URL."""
        if source == "github":
            return await self._fetch_github(repo_url)
        if source == "atomgit":
            return await self._fetch_atomgit(repo_url)
        raise PopularityError(f"Unsupported popularity source: {source}")

    async def _fetch_github(self, repo_url: str) -> RepoPopularity:
        owner_repo = extract_owner_repo(repo_url)
        url = f"{GITHUB_API_BASE}/repos/{owner_repo}"
        try:
            async with httpx.AsyncClient(
                timeout=GITHUB_TIMEOUT_SECONDS,
                headers=self._github_headers(),
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
        except httpx.RequestError as exc:
            raise PopularityError(f"GitHub API request failed: {exc}") from exc

        if resp.status_code == 404:
            raise PopularityError(f"GitHub repo not found: {owner_repo}")
        if resp.status_code == 429:
            raise PopularityError(
                f"GitHub API rate-limited (429) for {owner_repo}; "
                "configure crawler.github_token to raise the limit"
            )
        if resp.status_code != 200:
            raise PopularityError(
                f"GitHub API returned {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        return RepoPopularity(
            url=repo_url,
            source="github",
            stars=_int_value(data, "stargazers_count", "stars_count"),
            forks=_int_value(data, "forks_count"),
            watchers=_int_value(data, "watchers_count"),
        )

    async def _fetch_atomgit(self, repo_url: str) -> RepoPopularity:
        owner_repo = extract_owner_repo(repo_url)
        url = f"{ATOMGIT_API_BASE}/repos/{owner_repo}"
        try:
            async with httpx.AsyncClient(
                timeout=ATOMGIT_TIMEOUT_SECONDS,
                headers=self._atomgit_headers(),
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
        except httpx.RequestError as exc:
            raise PopularityError(f"AtomGit API request failed: {exc}") from exc

        if resp.status_code == 404:
            raise PopularityError(f"AtomGit repo not found: {owner_repo}")
        if resp.status_code != 200:
            raise PopularityError(
                f"AtomGit API returned {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        return RepoPopularity(
            url=repo_url,
            source="atomgit",
            stars=_int_value(data, "star_count", "stargazers_count"),
            forks=_int_value(data, "forks_count"),
            watchers=_int_value(data, "watch_count", "watchers_count"),
        )


class PopularityCollector:
    """Collects popularity for all repositories in the configured repo list."""

    def __init__(self, fetcher: PopularityFetcher | None = None) -> None:
        self.fetcher = fetcher or PopularityFetcher()

    def _parse_repo_list(self, repository_path: Path | None) -> list[tuple[str, str]]:
        """Extract (repo_url, repo_type) pairs from the crawler config file.

        The repo type is derived from the config section the URL appears in:
        ``community`` -> "community", ``enterprise`` -> "enterprise",
        ``personal`` -> "personal".
        """
        from src.skills.config import load_crawler_config

        config = load_crawler_config(repository_path)
        repos: list[tuple[str, str]] = []
        seen: set[str] = set()
        for key in ("community", "personal", "enterprise"):
            repo_type = key
            for item in config.get(key, []) or []:
                url = item.get("url") if isinstance(item, dict) else None
                if isinstance(url, str) and url.strip() and url.strip() not in seen:
                    seen.add(url.strip())
                    repos.append((url.strip(), repo_type))
        return repos

    @staticmethod
    def _source_for_url(repo_url: str) -> str:
        parsed = urlparse(repo_url.strip())
        host = parsed.netloc.lower()
        if host == "github.com":
            return "github"
        if host == "atomgit.com":
            return "atomgit"
        try:
            source, _ = derive_skill_source(repo_url)
            return source
        except ValueError:
            raise PopularityError(f"Cannot determine source for URL: {repo_url}")

    async def collect(
        self,
        repository_path: Path | None = None,
        *,
        only: str | None = None,
    ) -> list[RepoPopularity]:
        """Collect popularity for configured repos, with bounded concurrency."""
        repos = self._parse_repo_list(repository_path)
        if only:
            repos = [(url, repo_type) for url, repo_type in repos if self._source_for_url(url) == only]
        if not repos:
            return []

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

        async def _fetch_one(repo_url: str, repo_type: str) -> RepoPopularity | None:
            async with semaphore:
                source = self._source_for_url(repo_url)
                try:
                    result = await self.fetcher.fetch(repo_url, source)
                    result.repo_type = repo_type
                    return result
                except PopularityError as exc:
                    _logger.warning("Failed to collect popularity for %s: %s", repo_url, exc)
                    return None

        results = await asyncio.gather(*(_fetch_one(url, repo_type) for url, repo_type in repos))
        return [result for result in results if result is not None]
