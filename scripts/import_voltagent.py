#!/usr/bin/env python3
import re
import subprocess
import sys
from sqlalchemy import text
sys.path.insert(0, '/home/ubuntu/skillhub')

from core.database import SyncSessionLocal
from src.models.orm import Skill
from core.config import get_settings

CATEGORIES = [
    "git-and-github",
    "marketing-and-sales",
    "communication",
    "coding-agents-and-ides",
    "productivity-and-tasks",
    "speech-and-transcription",
    "browser-and-automation",
    "ai-and-llms",
    "smart-home-and-iot",
    "web-and-frontend-development",
    "data-and-analytics",
    "shopping-and-e-commerce",
    "devops-and-cloud",
    "calendar-and-scheduling",
    "image-and-video-generation",
    "media-and-streaming",
    "pdf-and-documents",
    "apple-apps-and-services",
    "notes-and-pkm",
    "self-hosted-and-automation",
    "search-and-research",
    "ios-and-macos-development",
    "security-and-passwords",
    "clawdbot-tools",
    "transportation",
    "moltbook",
    "cli-utilities",
    "personal-development",
    "gaming",
    "health-and-fitness",
]

BASE_URL = "https://raw.githubusercontent.com/VoltAgent/awesome-openclaw-skills/main/categories"


def parse_skills_from_markdown(content: str, category: str):
    skills = []
    lines = content.split("\n")

    for line in lines:
        line = line.strip()
        if line.startswith("- [") and "](" in line:
            match = re.match(r"- \[([^\]]+)\]\(([^)]+)\)(?:\s*-\s*(.*))?", line)
            if match:
                name = match.group(1).strip()
                url = match.group(2).strip()
                description = match.group(3).strip() if match.group(3) else None

                if url.startswith("https://clawskills.sh/"):
                    source = "clawhub"
                    source_url = url
                elif url.startswith("https://github.com/"):
                    source = "github"
                    source_url = url
                else:
                    source = "unknown"
                    source_url = url

                skill_id = extract_skill_id(url, name)
                category_name = category.replace("-", " ").title()

                skills.append({
                    "skill_id": skill_id,
                    "name": name,
                    "description": description,
                    "source": source,
                    "source_url": source_url,
                    "category": category_name,
                })

    return skills


def extract_skill_id(url: str, name: str) -> str:
    if "clawskills.sh" in url:
        parts = url.split("/")
        if len(parts) >= 5:
            author = parts[-2]
            skill_name = parts[-1]
            return f"{author}/{skill_name}"
    elif "github.com" in url:
        match = re.search(r"github\.com/([^/]+)/([^/]+)", url)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return name.lower().replace(" ", "-")


def fetch_category(category: str) -> str:
    url = f"{BASE_URL}/{category}.md"
    try:
        result = subprocess.run(
            ["curl", "-s", url],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.stdout
    except Exception as e:
        print(f"Error fetching {category}: {e}")
        return ""


def main():
    total_skills = 0
    imported = 0
    skipped = 0

    session = SyncSessionLocal()

    for category in CATEGORIES:
        print(f"Fetching category: {category}...", end=" ")
        content = fetch_category(category)
        if not content:
            print("FAILED")
            continue

        skills = parse_skills_from_markdown(content, category)
        print(f"Found {len(skills)} skills")

        for skill_data in skills:
            try:
                existing = session.query(Skill).filter(
                    Skill.skill_id == skill_data["skill_id"]
                ).first()

                if existing:
                    skipped += 1
                    continue

                skill = Skill(
                    skill_id=skill_data["skill_id"],
                    name=skill_data["name"],
                    description=skill_data["description"],
                    source=skill_data["source"],
                    source_url=skill_data["source_url"],
                    category=skill_data["category"],
                )
                session.add(skill)
                imported += 1
            except Exception as e:
                print(f"Error importing {skill_data['skill_id']}: {e}")
                skipped += 1

        total_skills += len(skills)

    session.commit()
    print(f"\nTotal: {total_skills} skills found, {imported} imported, {skipped} skipped")


if __name__ == "__main__":
    main()
