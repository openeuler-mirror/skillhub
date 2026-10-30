#!/usr/bin/env python3
"""
Generate test data for WittyHub
Creates 200 skills with multiple versions and realistic content

Usage:
    python scripts/generate_test_data.py [--count 200] [--host localhost] [--port 5432]
"""

import argparse
import random
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

CATEGORIES = [
    "Development", "Cloud", "Security", "AI", "Data", "Design",
    "DevOps", "Mobile", "Backend", "Frontend", "Database", "Networking"
]

PLATFORMS = [
    "python", "javascript", "typescript", "go", "rust", "java",
    "csharp", "ruby", "php", "swift", "kotlin", "scala"
]

SOURCES = ["github", "gitlab", "atomgit", "local", "clawhub"]

SKILL_TEMPLATES = {
    "python": {
        "name_prefix": ["Python", "Py"],
        "name_suffix": ["Framework", "SDK", "CLI", "Library", "Toolkit", "API", "Server", "Client"],
        "tags": ["python", "aiohttp", "fastapi", "django", "flask", "pydantic", "sqlalchemy"]
    },
    "javascript": {
        "name_prefix": ["JS", "JavaScript", "Node"],
        "name_suffix": ["SDK", "CLI", "Library", "Toolkit", "API", "Server", "Client", "Starter"],
        "tags": ["javascript", "nodejs", "npm", "express", "react", "vue", "webpack"]
    },
    "typescript": {
        "name_prefix": ["TS", "TypeScript"],
        "name_suffix": ["SDK", "CLI", "Library", "Toolkit", "API", "Framework", "Starter"],
        "tags": ["typescript", "nodejs", "zod", "prisma", "trpc", "nestjs"]
    },
    "go": {
        "name_prefix": ["Go", "Golang"],
        "name_suffix": ["SDK", "CLI", "Library", "Framework", "Toolkit", "Microservice"],
        "tags": ["golang", "grpc", "echo", "gin", "fiber"]
    },
    "rust": {
        "name_prefix": ["Rust"],
        "name_suffix": ["SDK", "CLI", "Library", "Framework", "Toolkit", "WASM"],
        "tags": ["rust", "tokio", "actix", "serde", "wasm"]
    },
}

AUTHORS = [
    "TechCorp Team", "OpenSource Collective", "DevTools Inc", "CloudNative Labs",
    "DataEng Studio", "AILab", "DevOps Masters", "Security First", "FullStack Pro",
    "Microservice Hub", "APIGuild", "CLI Wizards", "Framework Fans"
]

SKILL_README_TEMPLATES = [
    """# {name}

{description}

## Features

- 🚀 **High Performance** - Optimized for speed and efficiency
- 🔒 **Secure by Default** - Built-in security best practices
- 📦 **Easy Installation** - Simple one-command setup
- 📚 **Well Documented** - Comprehensive guides and API reference
- 🔧 **Highly Configurable** - Customize behavior to your needs

## Installation

```bash
# Using package manager
pip install {package_name}

# Or from source
git clone {source_url}
cd {repo_name}
pip install -e .
```

## Quick Start

```python
from {import_name} import Client

client = Client(api_key="your-api-key")
result = client.process(input_data)
print(result)
```

## Configuration

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| timeout | int | 30 | Request timeout in seconds |
| max_retries | int | 3 | Maximum retry attempts |
| debug | bool | false | Enable debug mode |

## API Reference

### Client

Main client class for interacting with the service.

```python
client = Client(config={{"option": "value"}})
```

### Methods

- `client.process(data)` - Process input data
- `client.validate(input)` - Validate input schema
- `client.get_status()` - Get current status

## License

MIT License - see LICENSE file for details.
""",
    """# {name}

{description}

## 🎯 Overview

{name} provides a complete solution for modern development workflows.

## Installation

```bash
npm install {package_name}
# or
yarn add {package_name}
```

## Usage

```typescript
import {{ Client }} from '{package_name}';

const client = new Client({{
  apiKey: process.env.API_KEY,
  baseUrl: 'https://api.example.com'
}});

const result = await client.execute({{
  input: 'your data here'
}});
```

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Client    │────▶│   Gateway   │────▶│   Service   │
└─────────────┘     └─────────────┘     └─────────────┘
```

## Examples

### Basic Example

```typescript
const response = await client.call('action', {{ param: 'value' }});
```

### Advanced Usage

```typescript
const options = {{
  timeout: 5000,
  retries: 3,
  cache: true
}};

const result = await client.advanced(action, payload, options);
```

## Contributing

Contributions are welcome! Please read our contributing guide first.

## License

Apache 2.0
""",
    """# {name}

{description}

## Overview

{name} is a powerful, production-ready solution designed for scale.

## Getting Started

```go
package main

import (
    "{module_path}"
    "context"
)

func main() {{
    client := {import_name}.NewClient(
        {import_name}.WithAPIKey(os.Getenv("API_KEY")),
    )

    resp, err := client.Process(context.Background(), &Request{{
        Data: "input",
    }})
    if err != nil {{
        panic(err)
    }}

    println(resp.Result)
}}
```

## Configuration

Environment variables:

- `API_KEY` - Your API key
- `BASE_URL` - API endpoint (optional)
- `DEBUG` - Enable debug logging

## Benchmark

| Operation | Latency | Throughput |
|-----------|---------|------------|
| Process | 5ms | 1000 req/s |
| Batch | 50ms | 500 items/s |

## License

MIT
""",
    """# {name}

{description}

## Features

- ✅ Type-safe and fully tested
- ✅ Async/await support
- ✅ Middleware pipeline
- ✅ Auto-reconnection
- ✅ Request batching
- ✅ Rate limiting

## Install

```bash
cargo add {crate_name}
```

## Quick Start

```rust
use {crate_name}::{{Client, Config}};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {{
    let client = Client::new(Config::default())?;

    let response = client.send("data").await?;
    println!("Result: {{}}", response);

    Ok(())
}}
```

## Advanced

### Custom Configuration

```rust
let config = Config::builder()
    .timeout(Duration::from_secs(30))
    .max_retries(3)
    .build();

let client = Client::with_config(config);
```

### Batch Processing

```rust
let items = vec!["a", "b", "c"];
let results = client.batch_process(items).await?;
```

## License

Apache-2.0 / MIT
"""
]


def generate_commit_id() -> str:
    """Generate a realistic commit hash"""
    return ''.join(random.choices('0123456789abcdef', k=40))

def generate_skill_content(name: str, platform: str) -> str:
    """Generate realistic README content"""
    template = random.choice(SKILL_README_TEMPLATES)
    package_name = name.lower().replace(" ", "-")
    repo_name = package_name
    module_path = f"github.com/example/{repo_name}"
    import_name = package_name.replace("-", "_")
    crate_name = package_name

    source_url = f"https://github.com/example/{repo_name}"
    content = template.format(
        name=name,
        description=f"Professional {name} solution for modern development",
        package_name=package_name,
        repo_name=repo_name,
        module_path=module_path,
        import_name=import_name,
        crate_name=crate_name,
        source_url=source_url
    )
    return content

def generate_skills(count: int) -> list:
    """Generate skill data"""
    skills = []
    used_skill_ids = set()
    used_version_keys = set()

    while len(skills) < count:
        platform = random.choice(PLATFORMS)
        template = SKILL_TEMPLATES.get(platform, SKILL_TEMPLATES["python"])
        prefix = random.choice(template["name_prefix"])
        suffix = random.choice(template["name_suffix"])
        name = f"{prefix} {suffix}"

        clean_name = name.lower().replace(" ", "-").replace("_", "-")

        category = random.choice(CATEGORIES)
        author = random.choice(AUTHORS)
        source = random.choice(SOURCES)
        base_url = f"https://{'github' if source == 'github' else source}.com/{author.lower().replace(' ', '')}/{clean_name}"

        num_versions = random.choices([1, 2, 3], weights=[40, 35, 25])[0]
        versions = []
        for v_idx in range(num_versions):
            major = random.randint(1, 5)
            minor = random.randint(0, 20)
            patch = random.randint(0, 50)
            versions.append(f"{major}.{minor}.{patch}")

        for version in versions:
            if len(skills) >= count:
                break

            skill_id = clean_name
            if skill_id in used_skill_ids:
                skill_id = f"{clean_name}-{uuid.uuid4().hex[:8]}"

            version_skill_key = f"{skill_id}:{version}"
            if version_skill_key in used_version_keys:
                continue

            used_skill_ids.add(skill_id)
            used_version_keys.add(version_skill_key)

            commit_id = generate_commit_id()

            skill = {
                "id": str(uuid.uuid4()),
                "skill_id": skill_id,
                "name": name,
                "description": f"A professional {name.lower()} solution with version {version}. "
                              f"Provides enterprise-grade features including advanced caching, "
                              f"rate limiting, and comprehensive error handling.",
                "version": version,
                "commit_id": commit_id,
                "author": author,
                "source": source,
                "source_url": base_url,
                "category": category,
                "tags": random.sample(template["tags"] + [platform, category.lower()],
                                     min(random.randint(3, 7), len(template["tags"]) + 2)),
                "platform": platform,
                "content": generate_skill_content(name, platform),
                "risk_score": random.choices(
                    range(0, 101),
                    weights=[3]*21 + [2]*30 + [1]*30 + [0.5]*20,  # bias toward safe/low risk
                    k=1
                )[0],
                "download_count": random.randint(100, 50000),
                "rating": str(round(random.uniform(3.5, 5.0), 2)),
                "created_at": datetime.now() - timedelta(days=random.randint(1, 365)),
                "updated_at": datetime.now() - timedelta(days=random.randint(0, 30)),
                "last_indexed_at": datetime.now() - timedelta(hours=random.randint(0, 48)),
                "extra_metadata": {
                    "license": random.choice(["MIT", "Apache-2.0", "BSD-3-Clause", "GPL-3.0"]),
                    "stars": random.randint(100, 50000),
                    "forks": random.randint(10, 5000),
                    "open_issues": random.randint(0, 100),
                    "language": platform.capitalize()
                }
            }
            skills.append(skill)

    return skills[:count]

def insert_skills(conn, skills):
    """Insert skills into database"""
    cursor = conn.cursor()

    # Clear existing data
    cursor.execute("TRUNCATE TABLE security_audits CASCADE")
    cursor.execute("TRUNCATE TABLE download_history CASCADE")
    cursor.execute("TRUNCATE TABLE skills CASCADE")
    cursor.execute("TRUNCATE TABLE skill_repos CASCADE")
    cursor.execute("TRUNCATE TABLE agents CASCADE")
    conn.commit()

    # Insert source repositories first so skills can reference them.
    repositories_by_url = OrderedDict()
    for skill in skills:
        source_url = skill["source_url"]
        if source_url not in repositories_by_url:
            repo_name = source_url.rstrip("/").split("/")[-1]
            # Deduplicate repo_name to avoid unique constraint violation
            original_name = repo_name
            suffix = 0
            while any(r["repo_name"] == repo_name for r in repositories_by_url.values()):
                suffix += 1
                repo_name = f"{original_name}-{suffix}"
            repositories_by_url[source_url] = {
                "id": str(uuid.uuid4()),
                "repo_name": repo_name,
                "source": skill["source"],
                "branch": skill["version"] or "main",
                "url": source_url,
                "local_path": None,
                "skill_discover_status": "done",
                "skill_num": 0,
                "created_at": datetime.now(),
                "updated_at": datetime.now(),
            }
        repositories_by_url[source_url]["skill_num"] += 1

    repository_insert_query = """
        INSERT INTO skill_repos (
            id, repo_name, source, branch, url, local_path,
            skill_discover_status, skill_num, created_at, updated_at
        ) VALUES %s
    """
    repository_values = [
        (
            repo["id"],
            repo["repo_name"],
            repo["source"],
            repo["branch"],
            repo["url"],
            repo["local_path"],
            repo["skill_discover_status"],
            repo["skill_num"],
            repo["created_at"],
            repo["updated_at"],
        )
        for repo in repositories_by_url.values()
    ]
    execute_values(cursor, repository_insert_query, repository_values)
    conn.commit()

    # Insert skills
    insert_query = """
        INSERT INTO skills (
            id, skill_repo_id, skill_id, name, description, version, commit_id, author, source,
            source_url, category, tags, platform, content, risk_score,
            download_count, rating, created_at, updated_at, last_indexed_at, extra_metadata
        ) VALUES %s
    """

    values = [
        (
            s["id"], repositories_by_url[s["source_url"]]["id"], s["skill_id"], s["name"], s["description"], s["version"],
            s["commit_id"], s["author"], s["source"], s["source_url"], s["category"],
            s["tags"], s["platform"], s["content"], s["risk_score"],
            s["download_count"], s["rating"], s["created_at"], s["updated_at"],
            s["last_indexed_at"], psycopg2.extras.Json(s["extra_metadata"])
        )
        for s in skills
    ]

    execute_values(cursor, insert_query, values)
    conn.commit()

    # Generate security audits for some skills
    print("Generating security audits...")
    cursor.execute("SELECT id, skill_id, risk_score FROM skills ORDER BY RANDOM() LIMIT %s",
                   (len(skills) * 3 // 4,))
    skills_for_audit = cursor.fetchall()

    audits = []
    risk_levels = ["low", "medium", "high", "critical"]
    risk_signals_pool = [
        {"id": "sig1", "name": "Outdated dependencies", "description": "Package has known vulnerabilities", "severity": "High", "data": {}},
        {"id": "sig2", "name": "Weak crypto", "description": "Uses deprecated cryptographic algorithms", "severity": "Medium", "data": {}},
        {"id": "sig3", "name": "SQL injection risk", "description": "Potential SQL injection in query builder", "severity": "Critical", "data": {}},
        {"id": "sig4", "name": "XSS vulnerability", "description": "Unsanitized user input rendered", "severity": "High", "data": {}},
        {"id": "sig5", "name": "Missing rate limiting", "description": "No rate limiting on public endpoints", "severity": "Medium", "data": {}},
        {"id": "sig6", "name": "Secure configuration", "description": "Security headers properly configured", "severity": "Low", "data": {}},
        {"id": "sig7", "name": "Input validation", "description": "All inputs properly validated", "severity": "Low", "data": {}},
        {"id": "sig8", "name": "API key exposure", "description": "API key logged in plaintext", "severity": "Critical", "data": {}},
    ]

    for skill_id, skill_skill_id, risk_score in skills_for_audit:
        risk_level = "low" if risk_score <= 20 else "medium" if risk_score <= 50 else "high" if risk_score <= 80 else "critical"
        num_signals = random.randint(0, 3)
        signals = random.sample(risk_signals_pool, min(num_signals, len(risk_signals_pool)))

        audits.append((
            str(uuid.uuid4()), "skill", skill_id, None, None,
            "automated_scan", risk_level, psycopg2.extras.Json(signals),
            psycopg2.extras.Json({"scanner": "WittyHub Security Scanner v1.0"}),
            datetime.now() - timedelta(days=random.randint(0, 30))
        ))

    if audits:
        audit_query = """
            INSERT INTO security_audits (
                id, resource_type, resource_id, version, commit_id,
                audit_type, risk_level, risk_signals, details, audited_at
            ) VALUES %s
        """
        execute_values(cursor, audit_query, audits)
        conn.commit()

    cursor.close()
    return len(skills)

def main():
    parser = argparse.ArgumentParser(description="Generate test data for WittyHub")
    parser.add_argument("--count", type=int, default=200, help="Number of skills to generate")
    parser.add_argument("--host", default="localhost", help="Database host")
    parser.add_argument("--port", type=int, default=5432, help="Database port")
    parser.add_argument("--user", default="wittyhub", help="Database user")
    parser.add_argument("--db", default="wittyhub", help="Database name")
    parser.add_argument("--password", default="wittyhub123", help="Database password")
    args = parser.parse_args()

    print(f"=== Generating {args.count} test skills ===")

    # Connect to database
    try:
        conn = psycopg2.connect(
            host=args.host,
            port=args.port,
            user=args.user,
            dbname=args.db,
            password=args.password
        )
        print(f"Connected to database at {args.host}:{args.port}")
    except psycopg2.Error as e:
        print(f"Failed to connect to database: {e}")
        return 1

    # Generate skills
    print("Generating skill data...")
    skills = generate_skills(args.count)
    print(f"Generated {len(skills)} skills")

    # Insert into database
    print("Inserting into database...")
    inserted = insert_skills(conn, skills)
    print(f"Inserted {inserted} skills")

    # Verify
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM skills")
    count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(DISTINCT category) FROM skills")
    categories = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM security_audits")
    audits = cursor.fetchone()[0]
    cursor.close()
    conn.close()

    print("")
    print("=== Generation Complete ===")
    print(f"Total skills: {count}")
    print(f"Categories: {categories}")
    print(f"Security audits: {audits}")

    return 0

if __name__ == "__main__":
    exit(main())
