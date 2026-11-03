"""Skill frontmatter parsing, ID generation, and URL building."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from src.models.orm import SkillRepoModel


# ── URL building ───────────────────────────────────────────────────


def build_skill_md_url(
    repo: "SkillRepoModel",
    relative_path: str,
    ref: str | None = None,
) -> str | None:
    if not repo.url:
        return None
    browse_base_url = normalize_repository_browse_base_url(repo.url)
    if not browse_base_url:
        return None
    if ref and ref != 'HEAD':
        branch = ref
    elif repo.branch:
        branch = repo.branch
    else:
        # GitHub supports /blob/HEAD/ as a placeholder for the default branch;
        # other platforms (AtomGit, Gitee, GitLab) do not and need an actual
        # branch name.  Fall back to 'master' which is the most common default
        # on those platforms.
        branch = 'HEAD' if repo.source == 'github' else 'master'
    cleaned_relative_path = relative_path.lstrip('/')
    return f'{browse_base_url}/blob/{branch}/{cleaned_relative_path}'


def parse_skill_md_url(skill_url: str) -> tuple[str, str, str]:
    """解析单 SKILL.md 贡献 URL → ``(repo_url, ref, skill_path)``。

    格式为 ``<scheme>://<host>/<owner>/<repo>/blob/<ref>/<path>/SKILL.md``
    （见 openEuler-skills README 的 URL 贡献方式）；``ref`` 取 ``/blob/``
    后第一段，约定为单段分支名（含 ``/`` 的 ref 无法与路径消歧，不支持）。
    ``repo_url`` 经 ``normalize_git_clone_url`` 规范化，与 skill_repos 的
    存储口径一致。不符合格式时抛 ``ValueError``。
    """
    stripped = (skill_url or '').split('#', 1)[0].split('?', 1)[0].strip()
    if not stripped:
        raise ValueError('skill_url is required')
    if '/blob/' not in stripped:
        raise ValueError(f'skill_url must contain /blob/<ref>/<path>: {skill_url}')
    repo_part, _, ref_path = stripped.partition('/blob/')
    ref, _, skill_path = ref_path.partition('/')
    if not ref or not skill_path:
        raise ValueError(
            f'skill_url must be <repo>/blob/<ref>/<path>/SKILL.md: {skill_url}'
        )
    if skill_path != 'SKILL.md' and not skill_path.endswith('/SKILL.md'):
        raise ValueError(f'skill_url must point to a SKILL.md file: {skill_url}')
    repo_url = normalize_git_clone_url(repo_part)
    if not repo_url:
        raise ValueError(f'Invalid skill_url repository part: {skill_url}')
    return repo_url, ref, skill_path


def normalize_repository_browse_base_url(repo_url: str) -> str | None:
    normalized_url = repo_url.strip()
    if not normalized_url:
        return None

    ssh_match = re.match(r'git@([^:]+):(.+)', normalized_url)
    if ssh_match:
        host = ssh_match.group(1)
        path = ssh_match.group(2).strip('/')
        if path.endswith('.git'):
            path = path[:-4]
        return f'https://{host}/{path}'

    parsed = urlparse(normalized_url)
    if not parsed.netloc:
        return None
    path = parsed.path.strip('/')
    if path.endswith('.git'):
        path = path[:-4]
    if not path:
        return None
    return f'{parsed.scheme}://{parsed.netloc}/{path}'


# ── Skill source derivation ────────────────────────────────────────


def derive_skill_source(repo_url: str | None) -> tuple[str, str]:
    if not repo_url:
        raise ValueError('Repository url is required to derive skill source')

    normalized_url = repo_url.strip()
    ssh_match = re.match(r'git@([^:]+):(.+)', normalized_url)
    if ssh_match:
        raw_host = ssh_match.group(1).lower()
        path = ssh_match.group(2).strip('/')
    else:
        parsed = urlparse(normalized_url)
        if not parsed.netloc:
            raise ValueError(f'Invalid git repository url: {repo_url}')
        raw_host = parsed.netloc.lower()
        path = parsed.path.strip('/')

    if path.endswith('.git'):
        path = path[:-4]

    if raw_host == 'github.com':
        host = 'github'
    elif raw_host == 'atomgit.com':
        host = 'atomgit'
    elif raw_host == 'gitcode.com':
        host = 'atomgit'
    elif raw_host == 'gitlab.com':
        host = 'gitlab'
    elif raw_host == 'gitee.com':
        host = 'gitee'
    else:
        raise ValueError(f'Unsupported git repository host: {repo_url}')

    segments = [segment for segment in path.split('/') if segment]
    if len(segments) < 2:
        raise ValueError(f'Failed to extract repository owner from url: {repo_url}')

    owner = segments[-2].strip()
    if not owner:
        raise ValueError(f'Invalid repository owner derived from url: {repo_url}')

    return host, owner


# ── Frontmatter parsing ────────────────────────────────────────────


def load_skill_frontmatter(
    skill_file: Path,
) -> tuple[dict[str, object], str]:
    text = skill_file.read_text(encoding='utf-8')
    return parse_skill_frontmatter_text(text)


def parse_skill_frontmatter_text(text: str) -> tuple[dict[str, object], str]:
    stripped = text.lstrip()
    if not stripped.startswith('---'):
        return {}, text.strip()
    parts = stripped.split('---', maxsplit=2)
    if len(parts) < 3:
        return {}, text.strip()

    raw_frontmatter = parts[1]
    content = parts[2].lstrip('\r\n')

    metadata: dict[str, object] = {}
    current_key: str | None = None
    block_scalar_indicator: str | None = None
    for line in raw_frontmatter.splitlines():
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith('#'):
            continue

        # While in a YAML block scalar (| or >), any indented line is content;
        # a non-indented line ends the block and is processed normally.
        if block_scalar_indicator is not None:
            if line[:1] in (' ', '\t'):
                if current_key is not None:
                    existing = metadata.get(current_key)
                    if isinstance(existing, str) and existing:
                        if block_scalar_indicator == '|':
                            metadata[current_key] = f'{existing}\n{stripped_line}'
                        else:  # folded '>'
                            metadata[current_key] = f'{existing} {stripped_line}'
                    else:
                        metadata[current_key] = stripped_line
                continue
            block_scalar_indicator = None

        # YAML block list item: "- value" under any key (not just triggers)
        if stripped_line.startswith('- ') and current_key is not None:
            existing = metadata.get(current_key)
            if not isinstance(existing, list):
                # First list item — convert from scalar (usually empty
                # string) to list, preserving a non-empty scalar as the
                # first element (handles "key: val\n- item" edge case).
                existing = [existing] if isinstance(existing, str) and existing else []
                metadata[current_key] = existing
            item = stripped_line[2:].strip()
            if item:
                existing.append(item)
            continue

        if ':' not in line:
            if current_key is not None:
                existing = metadata.get(current_key)
                if isinstance(existing, str):
                    metadata[current_key] = f'{existing} {stripped_line}'.strip()
            continue

        key, value = line.split(':', 1)
        current_key = key.strip()
        value = value.strip()

        if value in ('|', '>', '|-', '>-', '|+', '>+'):
            block_scalar_indicator = value[0]
            metadata[current_key] = ''
            continue

        block_scalar_indicator = None
        metadata[current_key] = _parse_frontmatter_value(current_key, value)

    return metadata, content.strip()


def extract_declared_version(metadata: dict[str, object]) -> str | None:
    """提取 SKILL.md frontmatter 声明的版本号，兼容两种写法：

    - 顶层声明：``version: 1.0.0``
    - 嵌套声明（azure-skills 风格）：``metadata:`` 块下的 ``version``

    当前扁平解析器会把嵌套键提升为顶层键，两种写法都落在顶层；
    显式再查一次 ``metadata`` 字典，保证将来切换为保留嵌套结构的
    YAML 解析器时行为不变。顶层声明优先。
    """
    top_level = as_optional_str(metadata.get('version'))
    if top_level:
        return top_level
    nested = metadata.get('metadata')
    if isinstance(nested, dict):
        return as_optional_str(nested.get('version'))
    return None


def extract_declared_tags(metadata: dict[str, object]) -> list[str] | None:
    """提取 SKILL.md frontmatter 声明的 tags，归一化为去重后的字符串列表。

    取值位置与 ``extract_declared_version`` 一致（顶层优先，其次
    ``metadata:`` 嵌套块）。取值兼容三种写法：

    - YAML 列表：``tags: [a, b]`` 或块列表 ``- a``；每个元素是一个
      tag，元素内的空格属于 tag 名（如 ``- Extended Context``）
    - 逗号分隔字符串：``tags: "a, b, c"``（datadog-labs 风格）
    - 空格分隔字符串：``tags: "a b c"``（NVIDIA blueprint 风格）

    标量字符串按逗号和空白拆分（拆分后不再含空格，多词 tag 请用
    YAML 列表写法声明）；列表项仅按逗号拆分。拆分后 strip 去引号、
    去空项、保序去重；未声明或全部为空时返回 None。
    """
    raw = metadata.get('tags')
    if raw is None or raw == '' or raw == []:
        nested = metadata.get('metadata')
        if isinstance(nested, dict):
            raw = nested.get('tags')
    return _normalize_tag_list(raw)


def _normalize_tag_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        # 列表项是结构化声明，仅按逗号拆分，保留项内空格（多词 tag）
        parts = (part for item in value for part in str(item).split(','))
    else:
        # 标量字符串是分隔符拼接的列表，逗号和空白均为分隔符
        parts = re.split(r'[,\s]+', str(value))
    tags: list[str] = []
    seen: set[str] = set()
    for part in parts:
        tag = part.strip().strip('"\'').strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags or None


def _parse_frontmatter_value(key: str, value: str) -> object:
    if not value:
        return [] if key == 'triggers' else ''

    if key == 'triggers':
        if value.startswith('[') and value.endswith(']'):
            try:
                parsed = json.loads(value.replace("'", '"'))
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [value.strip('"\'')] if value.strip('"\'') else []

    lowered = value.lower()
    if lowered == 'true':
        return True
    if lowered == 'false':
        return False

    # Inline array syntax [a, b, c] — YAML flow sequence, applies to all keys
    if value.startswith('[') and value.endswith(']'):
        inner = value[1:-1].strip()
        if not inner:
            return []
        items = [item.strip().strip('"\'') for item in inner.split(',')]
        return [item for item in items if item] or []

    return value.strip('"\'' )


# ── Misc helpers ───────────────────────────────────────────────────


def derive_repository_skill_name(
    skill_file: Path, metadata: dict[str, object],
) -> str:
    metadata_name = metadata.get('name')
    if isinstance(metadata_name, str) and metadata_name.strip():
        return metadata_name.strip()
    if skill_file.name == 'SKILL.md':
        return skill_file.parent.name
    return skill_file.stem


# 扫描时需要跳过的目录规则：
# - 单层目录直接写目录名，匹配路径中任意一级目录（'docs' 匹配 'a/docs/SKILL.md'）
# - 多层目录用 '/' 连接，匹配路径中任意位置的连续片段
#   （'community/sig/demo' 匹配 'community/sig/demo/xxx/SKILL.md'）
SKILL_SKIP_PATH_RULES: tuple[str, ...] = (
    'template', 'templates', 'example', 'examples',
    'demo', 'demos', 'test', 'tests',
    'fixture', 'fixtures', 'docs', 'doc',
    'archive', 'archives', 'legacy',
)
# strip 首尾 '/'，容忍 'a/b/' 这类带尾斜杠的写法
_SKIP_RULE_PARTS: tuple[tuple[str, ...], ...] = tuple(
    rule.strip('/').lower().split('/') for rule in SKILL_SKIP_PATH_RULES
)


def should_skip_relative_path(relative_path: str) -> bool:
    directory_parts = relative_path.lower().split('/')[:-1]
    for rule_parts in _SKIP_RULE_PARTS:
        rule_length = len(rule_parts)
        if any(
            directory_parts[index:index + rule_length] == rule_parts
            for index in range(len(directory_parts) - rule_length + 1)
        ):
            return True
    return False


# 无效 skill 内容判定（扫描期内容门禁）：
# - 空洞内容：正文长度低于阈值，多为占位文件或测试桩
MIN_SKILL_CONTENT_LENGTH = 100

def is_low_value_skill_content(content: str) -> bool:
    """扫描时跳过正文过短内容无效的skill。"""
    text = content.strip()
    return len(text) < MIN_SKILL_CONTENT_LENGTH


# skill 元数据门禁：frontmatter 必须同时声明 name 与 description，
# 缺任一字段的 SKILL.md 不算 skill，扫描时跳过
REQUIRED_SKILL_METADATA_FIELDS: tuple[str, ...] = ('name', 'description')


def is_incomplete_skill_metadata(metadata: dict[str, object]) -> bool:
    """frontmatter 缺少必填元数据（name / description）时返回 True。"""
    return any(
        not str(metadata.get(field) or '').strip()
        for field in REQUIRED_SKILL_METADATA_FIELDS
    )


def normalize_git_clone_url(url: str | None) -> str:
    if not url:
        return ''
    stripped = url.strip()

    ssh_match = re.match(r'git@([^:]+):(.+)', stripped)
    if ssh_match:
        host = ssh_match.group(1)
        if host.lower() == 'gitcode.com':
            host = 'atomgit.com'
        path = ssh_match.group(2).strip('/')
        return f'git@{host}:{path}'

    parsed = urlparse(stripped)
    if not parsed.scheme or not parsed.netloc:
        return ''

    host = parsed.netloc
    if host.lower() == 'gitcode.com':
        host = 'atomgit.com'
    path = parsed.path.strip('/')
    if path.endswith('.git'):
        path = path[:-4]
    return f'{parsed.scheme}://{host}/{path}'


def normalize_clone_url_for_git(repo_url: str | None) -> str:
    if not repo_url:
        raise ValueError('git skill repos require url')
    if repo_url.endswith('.git'):
        return repo_url
    return f'{repo_url}.git'


def as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def as_optional_str_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        normalized = [str(item).strip() for item in value if str(item).strip()]
        return normalized or None
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else None
    return [str(value)]
