#!/usr/bin/env python3
"""同步 openEuler 非 src-openeuler 仓库中的 Skill 到 openEuler-skills/community。

流程：
1. 调用 openEuler 官网接口，一次性获取全部 SIG 及其仓库列表
2. 过滤掉 src-openeuler/* 仓库，其余仓库并发浅克隆到
   <storage.local_path>/skill-repositories/<host_owner_repo>，目录命名与
   skillhub 服务的仓库副本一致（如 atomgit.com_openeuler_sysboost），
   扫描后保留不删除，供后续扫描直接复用（再次运行时增量 fetch 更新）
3. 扫描仓库内文件名为 SKILL.md 的文件，frontmatter 必须同时声明
   name 与 description 才算有效 skill
4. 仓库含有效 skill → 登记到 openEuler-skills/community/<sig>/skill.yaml；
   已登记但本次扫描不再含 skill 的仓库 → 从 skill_repos 清理；
   已登记位置与官方 SIG 归属不一致 → 自动迁移到正确 SIG

用法：
    python scripts/sync_openeuler_skills.py --limit 20 --dry-run
    python scripts/sync_openeuler_skills.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.skills.skill_parser import is_incomplete_skill_metadata
from src.utils.git_operations import GitOperations
from src.skills.openeuler_sig import repo_name_from_openeuler_repo_slug
from src.core.config import get_settings

_logger = logging.getLogger('sync_openeuler_skills')

SIG_INFO_API = 'https://www.openeuler.org/api-magic/stat/sig/info'
REPO_CLONE_URL = 'https://atomgit.com/{slug}.git'

EXCLUDED_REPO_PREFIX = 'src-openeuler/'
CLONE_CONCURRENCY = 6
BLOB_FETCH_CONCURRENCY = 4

CATALOG_DIRNAME = 'openEuler-skills/community'
REPOS_DIRNAME = 'skill-repositories'

# openEuler-skills 是 catalog 登记簿仓库自身，不作为待扫描的 skill 来源仓库：
# 不 clone、不扫描、也不由本脚本自动增减其 skill_repos 登记（该登记由人工或
# catalog 侧的其他机制维护）。放在仓库根目录 skills/ 下的 skill 需另行消费。
CATALOG_REPO_SLUG = 'openeuler/openEuler-skills'


def is_catalog_repo_slug(slug: str) -> bool:
    """slug 是否为 catalog 登记簿仓库（openEuler-skills）自身。

    忽略大小写与 ``.git`` 尾缀，兼容 SIG 接口可能返回的不同写法。
    """
    return slug.strip().removesuffix('.git').lower() == CATALOG_REPO_SLUG.lower()


def storage_local_path() -> Path:
    """config.yaml 的 storage.local_path（WITTYHUB_CONFIG 可覆盖配置文件位置）。"""
    config_path = os.environ.get('WITTYHUB_CONFIG') or (
        Path(__file__).resolve().parent.parent / 'config.yaml'
    )
    local_path = Path(get_settings().storage.local_path).expanduser()
    _logger.info(
        'config: %s (exists=%s), storage.local_path=%s',
        config_path, Path(config_path).exists(), local_path,
    )
    return local_path


def default_catalog_dir() -> Path:
    """从 config.yaml 的 storage.local_path 推导 catalog 目录。

    路径为 ``<storage.local_path>/skill-repositories/openEuler-skills/community``。
    """
    catalog_dir = storage_local_path() / REPOS_DIRNAME / CATALOG_DIRNAME
    _logger.info('catalog dir derived: %s', catalog_dir)
    return catalog_dir


def default_repos_dir() -> Path:
    """仓库克隆目录：<storage.local_path>/skill-repositories。

    仓库按 host_owner_repo 平铺命名（如 atomgit.com_openeuler_sysboost），
    与 skillhub 服务的仓库副本同一命名空间；克隆结果保留不删除，
    供后续扫描直接复用。
    """
    repos_dir = storage_local_path() / REPOS_DIRNAME
    _logger.info('repos dir derived: %s', repos_dir)
    return repos_dir


@dataclass(slots=True)
class RepoTarget:
    sig_name: str
    slug: str  # 形如 openeuler/sysboost
    skill_paths: list[str] = field(default_factory=list)
    valid_skill_paths: list[str] = field(default_factory=list)
    # 扫描状态：cloned=克隆并扫描成功（结论可信）；clone_failed=克隆失败（不能据此清理登记）
    scan_status: str = 'pending'


def fetch_sig_repos(client: httpx.Client) -> list[tuple[str, dict[str, Any]]]:
    """拉取全部 SIG 元数据，返回 [(sig_name, sig_info)]。

    同时返回各 SIG 名下所有非 src-openeuler 仓库 slug（SIG 元数据里
    description 等字段供 skill.yaml 使用）。
    """
    _logger.info('fetching SIG info: GET %s', SIG_INFO_API)
    resp = client.get(SIG_INFO_API, params={'community': 'openeuler'}, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get('code') != 1:
        raise RuntimeError(f'sig info API error: {payload.get("message")}')

    results: list[tuple[str, dict[str, Any]]] = []
    total_repos = 0
    for sig in payload.get('data') or []:
        sig_name = (sig.get('name') or '').strip()
        if not sig_name:
            continue
        repos = [
            r.strip()
            for r in (sig.get('repositories') or [])
            if isinstance(r, str) and r.strip()
        ]
        total_repos += len(repos)
        sig['non_src_repos'] = [
            r for r in repos if not r.lower().startswith(EXCLUDED_REPO_PREFIX)
        ]
        results.append((sig_name, sig))
    _logger.info(
        'SIG info fetched: %d SIG(s), %d repos total, %d non-%s*',
        len(results), total_repos,
        sum(len(s.get('non_src_repos', [])) for _, s in results),
        EXCLUDED_REPO_PREFIX,
    )
    return results


def repo_clone_dir_name(slug: str) -> str:
    """仓库克隆目录名（host_owner_repo，如 atomgit.com_openeuler_sysboost）。

    口径与 src.skills.openeuler_sig.repo_name_from_openeuler_repo_slug
    一致，保证与 skillhub 服务的仓库副本同一命名空间。
    """
    name = repo_name_from_openeuler_repo_slug(slug)
    return name or f"atomgit.com_{slug}".replace('/', '_')


def sync_repo(git_ops: GitOperations, slug: str, dest: Path) -> bool:
    """克隆（或增量更新）仓库到 dest，成功返回 True。

    dest 已存在时只 fetch 更新、复用已有副本，供后续扫描直接使用。
    """
    clone_url = REPO_CLONE_URL.format(slug=slug)
    try:
        if (dest / '.git').exists():
            _logger.debug('repo copy exists, updating: %s', dest)
            git_ops.update_existing_repository(dest, clone_url, repo_url=clone_url)
        else:
            _logger.info('cloning repo: %s -> %s', slug, dest)
            git_ops.clone_repository(dest, clone_url, repo_url=clone_url)
        return True
    except subprocess.SubprocessError as exc:
        _logger.warning('clone/update failed: %s (%s)', slug, str(exc)[:200])
        return False


def list_skill_paths(git_ops: GitOperations, repo_dir: Path) -> list[str]:
    """列出仓库内待检查的 SKILL.md 相对路径。

    完全复用 git_operations.list_skill_paths_for_ref 的口径：
    只认文件名为 SKILL.md 的文件，且跳过 tests/docs/examples 等目录。
    """
    return git_ops.list_skill_paths_for_ref(repo_dir, 'HEAD')


def read_skill_metadata(
    git_ops: GitOperations, repo_dir: Path, rel_path: str,
) -> dict[str, Any] | None:
    """读取 SKILL.md 的 frontmatter，失败返回 None。"""
    result = git_ops.load_skill_frontmatter_from_git_ref(repo_dir, 'HEAD', rel_path)
    return result[0] if result is not None else None


def has_valid_skill_metadata(metadata: dict[str, Any]) -> bool:
    """frontmatter 必须同时声明 name 与 description 才算有效 skill。"""
    return not is_incomplete_skill_metadata(metadata)


async def scan_repos(
    targets: list[RepoTarget],
    repos_dir: Path,
    git_ops: GitOperations,
    clone_concurrency: int = CLONE_CONCURRENCY,
) -> None:
    """并发克隆/更新并扫描仓库，结果写回 targets 的 skill_paths/valid_skill_paths。

    仓库副本保留在 repos_dir 下（host_owner_repo 命名），供后续扫描复用。
    """
    clone_sem = asyncio.Semaphore(max(1, clone_concurrency))
    fetch_sem = asyncio.Semaphore(BLOB_FETCH_CONCURRENCY)
    total = len(targets)
    done = 0
    lock = asyncio.Lock()

    async def process(target: RepoTarget) -> None:
        nonlocal done
        dest = repos_dir / repo_clone_dir_name(target.slug)
        try:
            async with clone_sem:
                synced = await asyncio.to_thread(
                    sync_repo, git_ops, target.slug, dest,
                )
            if not synced:
                target.scan_status = 'clone_failed'
                return
            target.scan_status = 'cloned'
            skill_paths = await asyncio.to_thread(list_skill_paths, git_ops, dest)
            if not skill_paths:
                return
            _logger.info(
                '%s (%s): %d SKILL.md candidate(s)',
                target.slug, target.sig_name, len(skill_paths),
            )
            target.skill_paths = skill_paths

            async def check_one(rel_path: str) -> None:
                async with fetch_sem:
                    # 仓库级判定：已命中一个有效 skill 就足以登记，
                    # 跳过剩余 SKILL.md，省去无谓的 blob 拉取
                    if target.valid_skill_paths:
                        return
                    metadata = await asyncio.to_thread(
                        read_skill_metadata, git_ops, dest, rel_path,
                    )
                if metadata is None:
                    _logger.warning(
                        'skill hit check skipped (read failed): %s:%s',
                        target.slug, rel_path,
                    )
                    return
                if has_valid_skill_metadata(metadata):
                    target.valid_skill_paths.append(rel_path)
                    _logger.info('skill hit: %s -> %s', target.slug, rel_path)
                else:
                    _logger.debug(
                        'skill rejected (incomplete frontmatter): %s:%s',
                        target.slug, rel_path,
                    )

            await asyncio.gather(*(check_one(p) for p in skill_paths))
        finally:
            async with lock:
                done += 1
                if done % 50 == 0 or done == total:
                    _logger.info('scan progress: %d/%d repos processed', done, total)

    _logger.info(
        'scanning start: %d repo(s), clone_concurrency=%d',
        len(targets), max(1, clone_concurrency),
    )
    await asyncio.gather(*(process(t) for t in targets))

# ── skill.yaml 更新 ───────────────────────────────────────────────────


def render_skill_yaml(
    author: str,
    description: str | None,
    website: str | None,
    repos: list[dict[str, Any]],
    has_skills_section: bool,
) -> str:
    """按 openEuler-skills/templates/community-skill-spec.yaml 的格式输出。"""
    doc: dict[str, Any] = {
        'author': author,
        'description': description,
    }
    if website:
        doc['website'] = website
    doc['skill_repos'] = [{'url': u['url']} for u in repos] if repos else []
    if has_skills_section:
        doc['skills'] = []
    return yaml.safe_dump(
        doc,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=10000,
    )


def repo_url_from_slug(slug: str) -> str:
    """openeuler/sysboost → https://atomgit.com/openeuler/sysboost（与既有登记一致）。"""
    return f'https://atomgit.com/{slug}'


def update_sig_skill_yaml(
    sig_dir: Path,
    author: str,
    description: str | None,
    website: str | None,
    add_urls: list[str],
    remove_urls: list[str],
    dry_run: bool,
) -> tuple[bool, str]:
    """增量更新一个 SIG 的 skill.yaml，返回 (是否变化, 状态描述)。
    - add_urls 中的仓库追加进 skill_repos（已有则不重复登记）
    - remove_urls 中的仓库从 skill_repos 移除
    - 无变化时保持原文不动，避免无关 diff
    """
    yaml_path = sig_dir / 'skill.yaml'
    existing: dict[str, Any] = {}
    if yaml_path.exists():
        existing = yaml.safe_load(yaml_path.read_text(encoding='utf-8')) or {}

    old_urls = [
        str(r['url']).strip() if isinstance(r, dict) else str(r).strip()
        for r in (existing.get('skill_repos') or [])
    ]
    old_urls = [u for u in old_urls if u]
    old_url_set = set(old_urls)
    remove_set = set(remove_urls)

    kept = [u for u in old_urls if u not in remove_set]
    added = [u for u in add_urls if u not in old_url_set]
    removed_count = len(old_urls) - len(kept)
    if not added and not removed_count:
        return False, 'unchanged'

    final_urls = kept + added
    text = render_skill_yaml(
        author=existing.get('author') or author,
        description=existing.get('description', description),
        website=existing.get('website') or website,
        repos=[{'url': u} for u in final_urls],
        has_skills_section='skills' in existing,
    )
    if not dry_run:
        sig_dir.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text(text, encoding='utf-8')

    parts = []
    if added:
        parts.append(f'+{len(added)} repo(s)')
    if removed_count:
        parts.append(f'-{removed_count} repo(s)')
    return True, ', '.join(parts)


def plan_sig_changes(
    url_to_sig: dict[str, str],
    desired: dict[str, str],
    no_skill_urls: set[str],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """推导每个 sig 目录的 add / remove（含跨 SIG 迁移）。

    - url_to_sig: 已登记 url -> 当前所在 sig 目录名
    - desired: url -> 官方归属 SIG（有有效 skill 的仓库）
    - no_skill_urls: 扫描成功但无有效 skill 的 url，应从登记处清理
    - 返回 (add_by_sig, remove_by_sig)，key 为 sig 目录名
    """
    add_by_sig: dict[str, set[str]] = {}
    remove_by_sig: dict[str, set[str]] = {}

    for url, target_sig in desired.items():
        cur_sig = url_to_sig.get(url)
        if cur_sig is None:
            # 全新登记
            add_by_sig.setdefault(target_sig, set()).add(url)
        elif cur_sig != target_sig:
            # 登记错位：从旧 sig 移除，补充到官方归属的 sig
            remove_by_sig.setdefault(cur_sig, set()).add(url)
            add_by_sig.setdefault(target_sig, set()).add(url)
        # cur_sig == target_sig：位置正确，不动

    for url in no_skill_urls:
        cur_sig = url_to_sig.get(url)
        if cur_sig is not None:
            remove_by_sig.setdefault(cur_sig, set()).add(url)

    return add_by_sig, remove_by_sig


async def main() -> int:
    parser = argparse.ArgumentParser(
        description='同步 openEuler 非 src-openeuler 仓库中的 skill 到 openEuler-skills/community',
    )
    parser.add_argument(
        '--catalog-dir', type=Path, default=None,
        help='openEuler-skills/community 目录（默认取 config.yaml 的 '
             'storage.local_path/skill-repositories/openEuler-skills/community）',
    )
    parser.add_argument(
        '--limit', type=int, default=0,
        help='只扫描前 N 个仓库（默认 0 = 全量）',
    )
    parser.add_argument(
        '--concurrency', type=int, default=CLONE_CONCURRENCY,
        help=f'克隆并发数（默认 {CLONE_CONCURRENCY}）',
    )
    parser.add_argument('--dry-run', action='store_true', help='只打印变更，不写文件')
    parser.add_argument(
        '--repos-dir', type=Path, default=None,
        help='仓库克隆目录（默认取 config.yaml 的 '
             'storage.local_path/skill-repositories，按 host_owner_repo 平铺命名，'
             '扫描后保留供后续复用）',
    )
    parser.add_argument('--json', type=Path, help='把扫描结果导出为 JSON 文件')
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='输出 DEBUG 级日志（如 frontmatter 校验失败明细）',
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
    )
    _logger.info(
        'run config: dry_run=%s limit=%s concurrency=%d json=%s',
        args.dry_run, args.limit or 'all', args.concurrency,
        args.json or '-',
    )
    catalog_dir = args.catalog_dir or default_catalog_dir()
    if not catalog_dir.parent.exists():
        _logger.error('catalog dir not found: %s', catalog_dir)
        return 1
    _logger.info('catalog dir: %s', catalog_dir)

    with httpx.Client(follow_redirects=True, headers={'User-Agent': 'skillhub-sync/1.0'}) as client:
        sig_repos = fetch_sig_repos(client)

    targets: list[RepoTarget] = []
    seen_slugs: set[str] = set()
    for sig_name, info in sig_repos:
        for slug in info.get('non_src_repos', []):
            if is_catalog_repo_slug(slug):
                # catalog 仓库自身不进入扫描口径，避免被当成普通仓库 clone 一份
                continue
            # 同一仓库可能同时归属多个 SIG：按 slug 去重，只克隆/扫描一次，
            # 避免并发写同一目录；首次出现的 SIG 作为登记归属，
            # 避免 desired 映射被后遍历的 SIG 覆盖。
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            targets.append(RepoTarget(sig_name=sig_name, slug=slug))
    _logger.info(
        'SIGs: %d, non-src-openeuler repos: %d (slug-deduped)',
        len(sig_repos), len(targets),
    )

    if args.limit > 0:
        targets = targets[:args.limit]
        _logger.info('limited to first %d repos', len(targets))

    repos_dir = args.repos_dir or default_repos_dir()
    repos_dir.mkdir(parents=True, exist_ok=True)
    _logger.info('repos dir: %s', repos_dir)
    git_ops = GitOperations()
    await scan_repos(
        targets, repos_dir, git_ops,
        clone_concurrency=args.concurrency,
    )

    cloned_count = sum(1 for t in targets if t.scan_status == 'cloned')
    with_skill_md = sum(1 for t in targets if t.skill_paths)
    hit = [t for t in targets if t.valid_skill_paths]
    _logger.info(
        'scan done: %d/%d repos cloned, %d with SKILL.md, %d with valid skill(s)',
        cloned_count, len(targets), with_skill_md, len(hit),
    )

    if args.json:
        data = [
            {
                'sig': t.sig_name,
                'repo': t.slug,
                'scan_status': t.scan_status,
                'skill_paths': t.skill_paths,
                'valid_skill_paths': t.valid_skill_paths,
            }
            for t in targets
        ]
        args.json.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8',
        )
        _logger.info('scan report saved: %s', args.json)

    # ── 更新 openEuler-skills/community ──
    valid_by_sig: dict[str, set[str]] = {}
    confirmed_no_skill_by_sig: dict[str, set[str]] = {}
    for t in targets:
        if t.valid_skill_paths:
            valid_by_sig.setdefault(t.sig_name, set()).add(t.slug)
        elif t.scan_status == 'cloned':
            # 只有扫描成功的仓库才能得出"不含 skill"的结论，用于清理失效登记
            confirmed_no_skill_by_sig.setdefault(t.sig_name, set()).add(t.slug)

    # README 约定"同一个仓库只登记一次"。这里额外记录每个已登记 url 所属的 sig
    # 目录，以便扫描结果（官方 SIG 归属）与现有登记位置不一致时，自动把仓库
    # 从旧 SIG 目录迁移到官方归属的 SIG 目录（见 plan_sig_changes 的迁移分支）。
    url_to_sig: dict[str, str] = {}
    existing_yaml_count = 0
    for yaml_file in catalog_dir.glob('*/skill.yaml'):
        existing_yaml_count += 1
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding='utf-8')) or {}
        except yaml.YAMLError as exc:
            _logger.warning('skip unparseable skill.yaml: %s (%s)', yaml_file, exc)
            continue
        sig_dir_name = yaml_file.parent.name
        for entry in data.get('skill_repos') or []:
            url = entry.get('url') if isinstance(entry, dict) else entry
            if url:
                url_to_sig[str(url).strip()] = sig_dir_name
    _logger.info(
        'existing skill.yaml: %d file(s), %d url(s) already registered',
        existing_yaml_count, len(url_to_sig),
    )

    # 官方归属：有有效 skill 的仓库 → 目标 SIG；扫描成功但无 skill → 应清理
    desired: dict[str, str] = {}
    for sig_name, _info in sig_repos:
        for slug in sorted(valid_by_sig.get(sig_name, set())):
            desired[repo_url_from_slug(slug)] = sig_name
    no_skill_urls: set[str] = {
        repo_url_from_slug(slug)
        for slugs in confirmed_no_skill_by_sig.values()
        for slug in slugs
    }

    add_by_sig, remove_by_sig = plan_sig_changes(url_to_sig, desired, no_skill_urls)

    sig_meta = {name: info for name, info in sig_repos}
    changed_sigs = 0
    changed_sig_names = sorted(set(add_by_sig) | set(remove_by_sig))
    for sig_name in changed_sig_names:
        info = sig_meta.get(sig_name, {})
        website = f'https://www.openeuler.org/zh/sig/{sig_name}' if sig_name in sig_meta else None
        changed, status = update_sig_skill_yaml(
            sig_dir=catalog_dir / sig_name,
            author=sig_name,
            description=(info.get('description') or '').strip() or None,
            website=website,
            add_urls=sorted(add_by_sig.get(sig_name, set())),
            remove_urls=sorted(remove_by_sig.get(sig_name, set())),
            dry_run=args.dry_run,
        )
        if changed:
            changed_sigs += 1
            _logger.info('[%s] %s: %s', 'dry-run' if args.dry_run else 'updated', sig_name, status)

    total_valid = sum(len(v) for v in valid_by_sig.values())
    _logger.info(
        'done. %d SIG(s) %s. valid skill repos: %d',
        changed_sigs,
        'would change' if args.dry_run else 'changed',
        total_valid,
    )
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
