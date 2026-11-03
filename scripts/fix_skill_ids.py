#!/usr/bin/env python3
import re
import sys
sys.path.insert(0, '/home/ubuntu/skillhub')

from sqlalchemy import text
from core.database import SyncSessionLocal

def parse_clawskills_url(url):
    """Parse clawskills.sh URL correctly:
    https://clawskills.sh/skills/zanblayde-agent-commons -> (zanblayde, agent-commons)
    https://clawskills.sh/skills/wrannaman-agentdo -> (wrannaman, agentdo)
    """
    match = re.search(r'clawskills\.sh/skills/([^-]+)-(.+)$', url)
    if match:
        return match.group(1), match.group(2)
    return None, None

def main():
    session = SyncSessionLocal()

    result = session.execute(text("""
        SELECT id, skill_id, source_url
        FROM skills
        WHERE source_url LIKE '%clawskills.sh/skills%'
    """))

    for row in result:
        skill_id, source_url = row[0], row[2]
        author, skill = parse_clawskills_url(source_url)
        if author and skill:
            new_skill_id = f"{author}/{skill}"
            if new_skill_id != skill_id:
                print(f"{skill_id} -> {new_skill_id}")
                session.execute(text("""
                    UPDATE skills SET skill_id = :new_id WHERE id = :id
                """), {"new_id": new_skill_id, "id": skill_id})

    session.commit()
    print("\nDone!")

if __name__ == "__main__":
    main()