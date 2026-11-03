#!/usr/bin/env python3
import re
import subprocess
import sys
sys.path.insert(0, '/home/ubuntu/skillhub')

from core.database import SyncSessionLocal
from src.models.orm import Skill

def parse_clawhub_url(url: str):
    """Parse clawskills.sh URL to get author and skill_name

    https://clawskills.sh/skills/zanblayde-agent-commons
    -> author="zanblayde", skill_name="agent-commons"
    """
    match = re.search(r'clawskills\.sh/skills/([^-]+)-([^-]+)$', url)
    if match:
        return match.group(1), match.group(2)

    match = re.search(r'clawskills\.sh/skills/([a-zA-Z0-9]+)-(.+)$', url)
    if match:
        return match.group(1), match.group(2)
    return None, None


def main():
    session = SyncSessionLocal()

    clawhub_url = "https://clawskills.sh/skills/zanblayde-agent-commons"
    name = "agent-commons"
    description = "Consult, commit, extend, and challenge reasoning chains."
    category = "Git & GitHub"

    author, skill_name = parse_clawhub_url(clawhub_url)
    print(f"Parsed: author={author}, skill_name={skill_name}")

    skill_id = f"{author}/{skill_name}"
    skill_data = {
        "skill_id": skill_id,
        "name": name,
        "description": description,
        "source": "clawhub",
        "source_url": clawhub_url,
        "category": category,
    }
    print(f"\nSkill data: {skill_data}")

    try:
        skill = Skill(**skill_data)
        session.add(skill)
        session.commit()
        print(f"\nSuccessfully imported: {skill.skill_id}")
    except Exception as e:
        session.rollback()
        print(f"Error: {e}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
