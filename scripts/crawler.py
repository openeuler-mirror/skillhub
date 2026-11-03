#!/usr/bin/env python3
"""
Skill Crawler - Scrapes skills from clawhub and converts them to database-ready format.

功能:
1. 从clawhub获取skill列表和源仓库信息
2. 从源仓库下载SKILL.md内容
3. 写入数据库
"""

import asyncio
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional, List, Tuple, Dict
import httpx
import yaml


class SkillSource(str, Enum):
    CLAWHUB = "clawhub"
    GITHUB = "github"
    GITE = "gitee"
    ATOMGIT = "atomgit"


@dataclass
class SkillManifest:
    """Skill元数据"""
    skill_id: str
    name: str
    source: SkillSource
    source_url: str  # GitHub源仓库URL
    raw_skill_md_url: Optional[str] = None  # 原始SKILL.md URL
    description: Optional[str] = None
    category: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    author: Optional[str] = None
    version: Optional[str] = None


async def fetch_url(url: str, timeout: float = 30.0) -> Optional[str]:
    """异步获取URL内容"""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
            if response.status_code == 200:
                return response.text
    except Exception as e:
        print(f"Error fetching {url}: {e}")
    return None


async def extract_raw_url_from_clawhub_page(content: str) -> Tuple[Optional[str], Optional[str]]:
    """从clawhub页面提取源仓库URL和原始SKILL.md URL"""
    raw_url_match = re.search(r'"rawSkillMdUrl"\s*:\s*"([^"]+)"', content)
    github_url_match = re.search(r'"githubUrl"\s*:\s*"([^"]+)"', content)

    raw_url = raw_url_match.group(1) if raw_url_match else None
    github_url = github_url_match.group(1) if github_url_match else None

    return raw_url, github_url


async def crawl_manifests_from_awesome() -> List[SkillManifest]:
    """从 awesome-openclaw-skills 获取skill列表"""
    manifests = []

    CATEGORIES = [
        "ai-and-llms", "apple-apps-and-services", "browser-automation",
        "calendar-scheduling", "clawdbot-tools", "cli-utilities",
        "coding-agents-and-ides", "communication", "data-analytics",
        "devops-and-cloud", "gaming", "git-and-github", "health-fitness",
        "image-video-generation", "ios-macos-development", "marketing-sales",
        "media-streaming", "moltbook", "notes-pkm", "pdf-documents",
        "personal-development", "productivity-tasks", "search-research",
        "security-passwords", "self-hosted-automation", "shopping-ecommerce",
        "smart-home-iot", "speech-transcription", "transportation",
        "web-frontend-development"
    ]

    BASE_URL = "https://raw.githubusercontent.com/VoltAgent/awesome-openclaw-skills/main/categories"

    for category in CATEGORIES:
        print(f"Crawling category: {category}")
        content = await fetch_url(f"{BASE_URL}/{category}.md")
        if not content:
            continue

        lines = content.split("\n")
        category_name = category.replace("-", " ").title()

        for line in lines:
            line = line.strip()
            if line.startswith("- [") and "](" in line:
                match = re.match(r"- \[([^\]]+)\]\(([^)]+)\)(?:\s*-\s*(.*))?", line)
                if match:
                    name = match.group(1).strip()
                    url = match.group(2).strip()
                    description = match.group(3).strip() if match.group(3) else None

                    if "github.com" in url:
                        source = SkillSource.GITHUB
                        match2 = re.search(r"github\.com/([^/]+)/(.+)", url)
                        if match2:
                            owner = match2.group(1)
                            path = match2.group(2)
                            path = re.sub(r"(tree|blob)/main/", "", path)
                            path = path.rstrip("/")
                            if path.endswith("/SKILL.md"):
                                path = path[:-9]
                            path = re.sub(r"^skills/", "", path)
                            skill_id = f"github:{owner}/{path}"
                            source_url = url
                        else:
                            continue
                    else:
                        continue

                    manifests.append(SkillManifest(
                        skill_id=skill_id,
                        name=name,
                        source=source,
                        source_url=source_url,
                        description=description,
                        category=category_name,
                    ))

    print(f"Found {len(manifests)} skills from awesome list")
    return manifests


async def fetch_skill_detail_from_clawhub(skill_slug: str) -> Tuple[Optional[str], Optional[str]]:
    """从clawhub页面获取源仓库URL和原始SKILL.md URL"""
    url = f"https://clawskills.sh/skills/{skill_slug}"
    content = await fetch_url(url)
    if not content:
        return None, None
    return await extract_raw_url_from_clawhub_page(content)


async def extract_skill_content(manifest: SkillManifest) -> Optional[Dict[str, Any]]:
    """从源仓库提取SKILL.md内容，如果无法获取则使用manifest数据"""
    content = None
    if manifest.raw_skill_md_url:
        content = await fetch_url(manifest.raw_skill_md_url)
    elif manifest.source == SkillSource.GITHUB:
        url = manifest.source_url
        url = re.sub(r"github\.com/", "raw.githubusercontent.com/", url)
        url = re.sub(r"/(tree|blob)/main/", "/main/", url)
        if not url.endswith("/SKILL.md"):
            url = url.rstrip("/") + "/SKILL.md"
        content = await fetch_url(url)
    elif manifest.source == SkillSource.CLAWHUB:
        parts = manifest.skill_id.split(":")
        if len(parts) == 2:
            author_skill = parts[1]
            if "/" in author_skill:
                author = author_skill.split("/")[0]
                skill_name = author_skill.split("/")[-1]
            else:
                author = author_skill.split("-")[0] if "-" in author_skill else "unknown"
                skill_name = author_skill
            url = f"https://raw.githubusercontent.com/openclaw/skills/main/skills/{author}/{skill_name}/SKILL.md"
            content = await fetch_url(url)

    frontmatter = {}
    name = manifest.name
    description = manifest.description
    category = manifest.category
    tags = manifest.tags
    platforms: List[str] = []
    version = manifest.version

    if content:
        frontmatter = parse_frontmatter(content)
        name = frontmatter.get("name", name)
        description = frontmatter.get("description") or description or extract_description(content)
        category = frontmatter.get("category", category)
        tags = frontmatter.get("tags", tags)
        version = frontmatter.get("version", version)
        platforms = detect_platforms(content)

    if isinstance(tags, str):
        tags = [tags]

    return {
        "skill_id": manifest.skill_id,
        "name": name,
        "description": description,
        "content": content,
        "version": version,
        "commit_id": None,
        "author": manifest.author,
        "source": manifest.source.value if isinstance(manifest.source, Enum) else manifest.source,
        "source_url": manifest.source_url,
        "category": category,
        "tags": tags or [],
        "platform": ",".join(platforms) if platforms else None,
        "metadata": frontmatter,
    }


def parse_frontmatter(content: str) -> Dict[str, Any]:
    """解析SKILL.md的frontmatter"""
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    try:
        return yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}


def extract_description(content: str) -> str:
    """从markdown内容提取description"""
    lines = content.split("\n")
    description = []
    in_description = False

    for line in lines:
        if line.startswith("# "):
            in_description = True
            continue
        if in_description and line.startswith("##"):
            break
        if in_description and line.strip():
            description.append(line.strip())

    return " ".join(description)[:500]


def detect_platforms(content: str) -> List[str]:
    """检测skill支持的平台"""
    platforms = []
    lower_content = content.lower()

    if "python" in lower_content or ".py" in lower_content or "pip install" in lower_content:
        platforms.append("python")
    if "typescript" in lower_content or ".ts" in lower_content or "npm install" in lower_content:
        platforms.append("typescript")
    if "go" in lower_content or ".go" in lower_content or "go mod" in lower_content:
        platforms.append("go")

    return platforms


async def crawl_and_extract(manifests: List[SkillManifest], max_concurrent: int = 5) -> List[Dict[str, Any]]:
    """并发爬取skill内容"""
    results: List[Dict[str, Any]] = []
    semaphore = asyncio.Semaphore(max_concurrent)

    async def process(manifest: SkillManifest):
        async with semaphore:
            print(f"Processing: {manifest.skill_id}")
            content = await extract_skill_content(manifest)
            if content:
                results.append(content)
                print(f"  -> OK: {manifest.name}")
            else:
                print(f"  -> FAILED")

    await asyncio.gather(*[process(m) for m in manifests], return_exceptions=True)
    return results


async def import_skills_to_api(skills: List[Dict[str, Any]], api_base: str = "http://localhost:8080/api/v1") -> int:
    """Import skills to the API"""
    success = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for skill in skills:
            try:
                response = await client.post(f"{api_base}/skills/", json=skill)
                if response.status_code in (201, 409):
                    success += 1
                else:
                    print(f"  Error: {response.status_code} - {response.text[:100]}")
            except Exception as e:
                print(f"  Exception: {e}")
    return success


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skill Crawler")
    parser.add_argument("--output", default="skills_output.json", help="Output file")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of skills to crawl")
    parser.add_argument("--import", dest="do_import", action="store_true", help="Import to database via API")
    parser.add_argument("--api", default="http://localhost:8080/api/v1", help="API base URL")
    args = parser.parse_args()

    print("Crawling skill manifests from awesome-openclaw-skills...")
    manifests = await crawl_manifests_from_awesome()

    if args.limit > 0:
        manifests = manifests[:args.limit]

    print(f"\nExtracting content for {len(manifests)} skills...")
    contents = await crawl_and_extract(manifests)

    print(f"\nSuccessfully extracted {len(contents)} skills")

    if args.do_import:
        print(f"\nImporting to database...")
        imported = await import_skills_to_api(contents, args.api)
        print(f"Imported {imported} skills")
    else:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(contents, f, indent=2, ensure_ascii=False)
        print(f"Saved to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())