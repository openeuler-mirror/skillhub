import asyncio
import logging
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import InterfaceError
from sqlalchemy.sql.dml import Update

from src.skills.config import (
    load_catalog_skill_url_entries,
    load_crawler_config,
    load_single_catalog_entry,
)
from src.skills import skill_manager
from src.skills.openeuler_sig import _load_sig_mapping
from src.skills.skill_manager import SkillManager, SkillRepositoryRequest
from src.skills.skill_scanner import SkillScanner
from src.skills.discover_requests import (
    PLATFORM_CHOICES,
    _build_requests_from_config,
    build_requests_from_skill_yaml,
    _normalize_platform,
    build_configured_discover_requests,
)
from src.ai import security_backfill
from src.ai.category_classifier import (
    CATEGORY_RETRY_ATTEMPTS,
    CategoryClassificationError,
    DeepSeekCategoryClassifier,
)
from src.api.routes.health import router
from src.api.routes.skills import list_skills
from src.api.schemas.skill import SkillCreate, SkillListResponse, SkillResponse
from src.core.config import Settings, get_settings
from src.indexer.search import HYBRID_CANDIDATE_LIMIT, SearchService
from src.models.orm import Skill
from src.models.repository import SkillRepoRepository, SkillRepository
from src.utils.git_operations import MATERIALIZE_ATTEMPTS, GitCommandError, GitOperations
from src.skills.skill_id import build_skill_id, extract_owner_repo, resolve_relative_skill_path
from src.skills.skill_parser import (
    extract_declared_tags,
    extract_declared_version,
    parse_skill_frontmatter_text,
    parse_skill_md_url,
)


def _git(repository: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


# is_low_value_skill_content 会过滤正文 <100 字符的 SKILL.md，
# 所有扫描类测试夹具统一复用这段合法正文。
VALID_SKILL_BODY = (
    "# Skill\n\n## Overview\n"
    "This skill guides the user through a common developer workflow.\n"
    "It covers prerequisites, step-by-step instructions, and examples\n"
    "that can be applied directly to real projects without extra setup."
)


class TestSkillRepositoryUnit:
    def test_models_package_exports_skill_repo_types(self):
        from src.models import SkillRepoModel, SkillRepoRepository

        assert SkillRepoModel is not None
        assert SkillRepoRepository is not None

    @pytest.mark.asyncio
    async def test_increment_download_does_not_update_skill_updated_at(self):

        skill = SimpleNamespace(id=uuid.uuid4())
        session = AsyncMock()
        repo = SkillRepository(session)
        repo.get_by_skill_id = AsyncMock(return_value=skill)

        # 返回命中 Skill 的主键 UUID，供调用方落 DownloadHistory 记录
        assert await repo.increment_download("github/acme/example") == skill.id

        statement = session.execute.await_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert "download_count=(skills.download_count +" in sql
        assert "updated_at=" not in sql

    @pytest.mark.asyncio
    async def test_update_only_changes_updated_at_when_explicitly_requested(self):

        skill = SimpleNamespace(id=uuid.uuid4())
        session = AsyncMock()
        repo = SkillRepository(session)
        repo.get_by_skill_id = AsyncMock(side_effect=[skill, skill, skill, skill])

        await repo.update("github/acme/example", {"risk_score": 10})
        implicit_statement = session.execute.await_args_list[0].args[0]
        implicit_sql = str(implicit_statement.compile(dialect=postgresql.dialect()))
        assert "risk_score=" in implicit_sql
        assert "updated_at=" not in implicit_sql

        requested_time = datetime.now(timezone.utc)
        await repo.update(
            "github/acme/example",
            {"risk_score": 20, "updated_at": requested_time},
        )
        explicit_statement = session.execute.await_args_list[1].args[0]
        explicit_sql = str(explicit_statement.compile(dialect=postgresql.dialect()))
        assert "updated_at=" in explicit_sql

    @pytest.mark.asyncio
    async def test_store_skills_updates_existing_skill_and_updated_at(self):

        repo_id = uuid.uuid4()
        skill_id = "github:acme/example/example-skill"
        existing = SimpleNamespace(
            id=uuid.uuid4(),
            skill_repo_id=repo_id,
            skill_id=skill_id,
            commit_id="old-commit",
            tree_hash="old-tree",
            download_count=12,
            risk_score=77,
        )
        scanned = SimpleNamespace(
            skill_repo_id=repo_id,
            skill_id=skill_id,
            name="example-skill",
            description="Updated description",
            version=None,
            commit_id="new-commit",
            tree_hash="new-tree",
            author="acme",
            source="github",
            source_url="https://github.com/acme/example",
            repo_url="https://github.com/acme/example",
            category="Development",
            tags=["example"],
            platform="personal",
            extra_metadata={},
            content="# Updated",
            risk_score=10,
            rating=None,
        )
        existing_result = MagicMock()
        existing_result.scalars.return_value.all.return_value = [existing]
        versions_result = MagicMock()
        versions_result.scalars.return_value.all.return_value = []
        session = AsyncMock()
        session.execute.side_effect = [existing_result, MagicMock(), versions_result]

        await SkillRepository(session).store_skills_and_versions(
            repo_id,
            [scanned],
            [],
            commit=False,
        )

        update_statement = session.execute.await_args_list[1].args[0]
        compiled = update_statement.compile(dialect=postgresql.dialect())
        assert "UPDATE skills SET" in str(compiled)
        assert "updated_at=" in str(compiled)
        assert compiled.params["download_count"] == 12
        # 已审计的分数保留：不被新解析对象的 risk_score=10 覆盖
        assert compiled.params["risk_score"] == 77
        session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_skills_skips_unchanged_existing_skill(self):
        repo_id = uuid.uuid4()
        skill_id = "github:acme/example/example-skill"
        existing = SimpleNamespace(
            id=uuid.uuid4(),
            skill_repo_id=repo_id,
            skill_id=skill_id,
            commit_id="old-commit",
            tree_hash="same-tree",
            download_count=12,
        )
        scanned = SimpleNamespace(
            skill_repo_id=repo_id,
            skill_id=skill_id,
            commit_id="new-repo-commit",
            tree_hash="same-tree",
        )
        existing_result = MagicMock()
        existing_result.scalars.return_value.all.return_value = [existing]
        versions_result = MagicMock()
        versions_result.scalars.return_value.all.return_value = []
        session = AsyncMock()
        session.execute.side_effect = [existing_result, versions_result]

        await SkillRepository(session).store_skills_and_versions(
            repo_id,
            [scanned],
            [],
            commit=False,
        )

        assert session.execute.await_count == 2
        session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_skills_retries_once_on_dead_connection(self):
        """InterfaceError（连接被服务端断开）时换连接整体重放一次后成功。"""

        dead = InterfaceError(
            "SELECT skills.id, ...",
            (),
            Exception("the underlying connection is closed"),
        )
        empty_skills = MagicMock()
        empty_skills.scalars.return_value.all.return_value = []
        empty_versions = MagicMock()
        empty_versions.scalars.return_value.all.return_value = []
        session = AsyncMock()
        # 第一次落库首查即死连接；重放走完 skills/skill_versions 两次预加载
        session.execute.side_effect = [
            dead,
            empty_skills,
            empty_versions,
        ]

        result = await SkillRepository(session).store_skills_and_versions(
            uuid.uuid4(),
            [],
            [],
            commit=False,
        )

        assert result == ([], [])
        # 死连接被 close，重放复用同一 session（由连接池换新连接）
        session.close.assert_awaited_once()
        assert session.execute.await_count == 3
        session.flush.assert_awaited_once()
        session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_skills_retry_respects_commit_flag(self):
        """重放与首跑同样遵循 commit 开关（true 时一次 commit）。"""

        dead = InterfaceError("SELECT", (), Exception("connection closed"))
        empty_skills = MagicMock()
        empty_skills.scalars.return_value.all.return_value = []
        empty_versions = MagicMock()
        empty_versions.scalars.return_value.all.return_value = []
        session = AsyncMock()
        session.execute.side_effect = [dead, empty_skills, empty_versions]

        await SkillRepository(session).store_skills_and_versions(
            uuid.uuid4(),
            [],
            [],
            commit=True,
        )

        session.flush.assert_awaited_once()
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_store_skills_raises_when_retry_also_fails(self):
        """仅重试一次：重放再次遇到 InterfaceError 时照常上抛。"""

        session = AsyncMock()
        # side_effect 直接设为异常实例：每次调用都抛出
        session.execute.side_effect = InterfaceError(
            "SELECT", (), Exception("connection closed"),
        )

        with pytest.raises(InterfaceError):
            await SkillRepository(session).store_skills_and_versions(
                uuid.uuid4(),
                [],
                [],
            )

        # 首跑首查失败 → close → 重放首查再次失败，不再重试
        assert session.execute.await_count == 2
        session.close.assert_awaited_once()

    def test_list_with_skill_id_prefix_filters_by_repo(self):

        session = AsyncMock()
        repo = SkillRepository(session)
        query = repo._apply_skill_filters(
            select(Skill),
            Skill,
            source="github",
            skill_id_prefix="github:anthropics/claude-code",
        )
        compiled = query.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        # source_type 过滤 + repo 前缀过滤应同时生效
        assert "skills.source = " in sql
        assert "skills.skill_id LIKE " in sql
        assert "github:anthropics/claude-code/%" in compiled.params.values()

    @pytest.mark.asyncio
    async def test_list_skills_route_forwards_repo_filter(self):
        now = datetime.now(timezone.utc)
        skill = SimpleNamespace(
            id=str(uuid.uuid4()),
            skill_id="github:anthropics/claude-code/.ai/skills/add-or-fix-type-checking",
            name="add-or-fix-type-checking",
            description="Check types",
            version="1.0.0",
            commit_id="abc123",
            author="anthropics",
            source="github",
            source_url="https://github.com/anthropics/claude-code",
            repo_url=None,
            category="Development",
            tags=["types"],
            platform="personal",
            extra_metadata={},
            content=None,
            risk_score=10,
            download_count=3,
            _period_downloads=None,
            rating=None,
            created_at=now,
            updated_at=now,
            last_indexed_at=None,
        )
        skill_repository = MagicMock()
        skill_repository.list = AsyncMock(return_value=([skill], 1))
        db = AsyncMock()

        with patch("src.api.routes.skills.SkillRepository", return_value=skill_repository):
            response = await list_skills(
                skip=0,
                limit=20,
                category=None,
                platform=None,
                tags=None,
                security_level=None,
                source_type="github",
                repo="anthropics/claude-code",
                sort_by="updated_at",
                sort_period=None,
                db=db,
            )

        assert isinstance(response, SkillListResponse)
        kwargs = skill_repository.list.await_args.kwargs
        assert kwargs["source"] == "github"
        assert kwargs["skill_id_prefix"] == "github:anthropics/claude-code"
        assert kwargs["limit"] == 20

    def test_build_skill_id_uses_relative_skill_path(self):
        source = "github"
        repo_url = "https://github.com/wix/react-native-navigation.git"
        owner_repo = extract_owner_repo(repo_url)

        skill_id = build_skill_id(
            source, owner_repo, ".github/skills/rnn-codebase/SKILL.md",
        )

        assert skill_id == "github:wix/react-native-navigation/rnn-codebase"

    def test_build_skill_id_uses_repo_slug_for_root_skill(self):
        source = "github"
        repo_url = "https://github.com/acme/agent-skills.git"
        owner_repo = extract_owner_repo(repo_url)

        skill_id = build_skill_id(source, owner_repo, "SKILL.md")

        assert skill_id == "github:acme/agent-skills/agent-skills"

    def test_list_skill_paths_ignores_excluded_directories(self, tmp_path):
        for directory in ("docs", "examples", "tests", "templates"):
            skill_file = tmp_path / directory / "example" / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text("# Example", encoding="utf-8")

        _git(tmp_path, "init")
        _git(tmp_path, "config", "user.email", "tests@example.com")
        _git(tmp_path, "config", "user.name", "WittyHub Tests")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add excluded skills")

        manager = SkillManager(MagicMock(), MagicMock())

        assert manager._list_skill_paths(tmp_path) == []

    def test_list_skill_paths_accepts_scannable_skill(self, tmp_path):
        excluded_skill = tmp_path / "docs" / "SKILL.md"
        excluded_skill.parent.mkdir()
        excluded_skill.write_text("# Documentation", encoding="utf-8")
        valid_skill = tmp_path / "skills" / "release-helper" / "SKILL.md"
        valid_skill.parent.mkdir(parents=True)
        valid_skill.write_text("# Release Helper", encoding="utf-8")

        _git(tmp_path, "init")
        _git(tmp_path, "config", "user.email", "tests@example.com")
        _git(tmp_path, "config", "user.name", "WittyHub Tests")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add skills")

        manager = SkillManager(MagicMock(), MagicMock())

        assert "skills/release-helper/SKILL.md" in manager._list_skill_paths(tmp_path)

    def test_list_skill_paths_rejects_lookalike_skill_filenames(self, tmp_path):
        """文件名不是恰好 SKILL.md 的（如 _cannbot_SKILL.md）不算 skill，
        否则后续 build_skill_id 校验会抛错导致整仓扫描失败。"""

        lookalike = (
            tmp_path
            / "plugins-community"
            / "ascendc"
            / "reference"
            / "_cannbot_SKILL.md"
        )
        lookalike.parent.mkdir(parents=True)
        lookalike.write_text("# Reference only", encoding="utf-8")
        valid_skill = tmp_path / "skills" / "cannbot" / "SKILL.md"
        valid_skill.parent.mkdir(parents=True)
        valid_skill.write_text("# Cannbot", encoding="utf-8")

        _git(tmp_path, "init")
        _git(tmp_path, "config", "user.email", "tests@example.com")
        _git(tmp_path, "config", "user.name", "WittyHub Tests")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add skills")

        manager = SkillManager(MagicMock(), MagicMock())

        assert manager._list_skill_paths(tmp_path) == ["skills/cannbot/SKILL.md"]

    def test_skill_manager_workspace_defaults_to_storage_local_path(self, tmp_path):
        storage_path = str(tmp_path)
        with patch.object(skill_manager.settings.storage, "local_path", storage_path):
            manager = skill_manager.SkillManager(MagicMock(), MagicMock())

        assert manager.workspace_base == tmp_path.resolve()

    def test_skillcrawler_platform_choices_use_catalog_groups(self):
        assert PLATFORM_CHOICES == ("community", "enterprise", "personal")
        with pytest.raises(ValueError, match="expected community"):
            _normalize_platform("openeuler")

    def test_single_skill_yaml_builds_repo_and_skill_requests(self, tmp_path):
        """--url 单文件模式：读取指定 skill.yaml 的 skill_repos 与 skills[] 条目。"""

        skill_file = tmp_path / "community" / "sig-demo" / "skill.yaml"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text(
            "skill_repos:\n"
            "  - url: https://github.com/acme/tools\n"
            "skills:\n"
            "  - skill_name: one\n"
            "    skill_url: https://github.com/acme/solo/blob/main/skills/one/SKILL.md\n",
            encoding="utf-8",
        )

        requests = build_requests_from_skill_yaml(str(skill_file))

        assert len(requests) == 2
        repo_request = next(r for r in requests if r.skill_paths is None)
        skill_request = next(r for r in requests if r.skill_paths is not None)
        # platform / owner 取自目录结构（不再由 URL 推断）
        assert repo_request.url == "https://github.com/acme/tools"
        assert repo_request.platform == "community"
        assert repo_request.sig_name == "sig-demo"
        assert skill_request.url == "https://github.com/acme/solo"
        assert skill_request.branch == "main"
        assert skill_request.skill_paths == ["skills/one/SKILL.md"]

    def test_single_skill_yaml_resolves_catalog_relative_path(self, tmp_path, monkeypatch):
        """本地不存在的路径按 catalog 根解析（-r / 自动同步的 openEuler-skills）。"""

        skill_file = tmp_path / "personal" / "leon-wang2021" / "skill.yaml"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text(
            "skill_repos:\n- url: https://atomgit.com/leon-wang2021/aet\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "skillcrawler.main.sync_openEuler_skills_repo", lambda: tmp_path
        )

        requests = build_requests_from_skill_yaml("personal/leon-wang2021/skill.yaml")

        assert len(requests) == 1
        assert requests[0].url == "https://atomgit.com/leon-wang2021/aet"
        assert requests[0].platform == "personal"

    def test_single_skill_yaml_rejects_invalid_target(self, tmp_path, monkeypatch):
        """文件不存在 / 文件名不对 / 目录结构不符都抛 ValueError。"""

        monkeypatch.setattr(
            "skillcrawler.main.sync_openEuler_skills_repo", lambda: tmp_path
        )
        with pytest.raises(ValueError, match="skill.yaml not found"):
            build_requests_from_skill_yaml("personal/x/skill.yaml")

        wrong_name = tmp_path / "personal" / "x" / "other.yaml"
        wrong_name.parent.mkdir(parents=True)
        wrong_name.write_text("skill_repos: []\n", encoding="utf-8")
        with pytest.raises(ValueError, match="expected a skill.yaml file"):
            load_single_catalog_entry(wrong_name)

        bad_layout = tmp_path / "skill.yaml"
        bad_layout.write_text("skill_repos: []\n", encoding="utf-8")
        with pytest.raises(ValueError, match="must live at"):
            load_single_catalog_entry(bad_layout)

    def test_settings_env_overrides_yaml(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            """
postgres:
  host: localhost
ai:
  embedding_host: http://localhost:8081
security:
  enable_audit: true
model:
  api_key: local-key
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("POSTGRES__HOST", "db")
        monkeypatch.setenv("AI__EMBEDDING_HOST", "http://embedding:8081")
        monkeypatch.setenv("SECURITY__ENABLE_AUDIT", "false")
        monkeypatch.setenv("MODEL__API_KEY", "")

        settings = Settings.from_yaml(config_path)

        assert settings.postgres.host == "db"
        assert settings.ai.embedding_host == "http://embedding:8081"
        assert settings.security.enable_audit is False
        assert settings.model.api_key == ""

    def test_openeuler_sig_mapping_uses_only_openeuler_repos(self, tmp_path):
        sig_dir = tmp_path / "sig" / "sig-ops"
        sig_dir.mkdir(parents=True)
        (sig_dir / "sig-info.yaml").write_text(
            """
name: sig-ops
repositories:
- repo:
  - openeuler/PilotGo-plugin-llmops
  - src-openeuler/ignored-package
  - openeuler/intel-openvino
""",
            encoding="utf-8",
        )

        mapping = _load_sig_mapping(tmp_path)

        assert mapping["atomgit.com_openeuler_PilotGo-plugin-llmops"] == "sig-ops"
        assert mapping["atomgit.com_openeuler_intel-openvino"] == "sig-ops"
        assert "atomgit.com_src-openeuler_ignored-package" not in mapping

    def test_build_requests_from_catalog_includes_community_sig_name(self, tmp_path):
        repos_dict = {
            "community": [
                {
                    "url": "https://atomgit.com/openeuler/PilotGo-plugin-llmops",
                    "sig_name": "sig-ops",
                }
            ]
        }

        requests = _build_requests_from_config(
            repos_dict,
            "community",
        )

        assert requests[0].platform == "community"
        assert requests[0].sig_name == "sig-ops"

    def test_load_crawler_config_reads_catalog_directory_names(self, tmp_path):
        skill_file = tmp_path / "community" / "sig-security-facility" / "skill.yaml"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text(
            "name: security\nskill_repos:\n  - url: https://atomgit.com/openeuler/security\n",
            encoding="utf-8",
        )
        config = load_crawler_config(tmp_path)
        assert config["community"][0]["url"] == "https://atomgit.com/openeuler/security"
        assert config["community"][0]["sig_name"] == "sig-security-facility"

    def test_skill_manager_resolves_openeuler_sig_lazily(self):
        manager = SkillManager(MagicMock(), MagicMock())
        manager._openeuler_sig_by_repo_name = {
            "atomgit.com_openeuler_PilotGo-plugin-llmops": "sig-ops"
        }

        sig_name = manager._get_openeuler_sig_name(
            "atomgit.com_openeuler_PilotGo-plugin-llmops"
        )

        assert sig_name == "sig-ops"

    def test_full_repository_update_uses_regular_fetch(self, tmp_path):

        git_ops = GitOperations()
        git_ops._run_git_command_with_retries = MagicMock()
        git_ops._run_git_command_with_auth_retry = MagicMock()
        git_ops.is_shallow_repository = MagicMock(return_value=False)

        git_ops.update_existing_repository(
            tmp_path,
            "https://github.com/acme/repository.git",
            branch="main",
        )

        fetch_command = git_ops._run_git_command_with_auth_retry.call_args.args[0]
        assert fetch_command == [
            "git", "-C", str(tmp_path), "fetch", "origin", "main",
        ]

    def test_shallow_repository_update_keeps_depth_one_fetch(self, tmp_path):

        git_ops = GitOperations()
        git_ops._run_git_command_with_retries = MagicMock()
        git_ops._run_git_command_with_auth_retry = MagicMock()
        git_ops.is_shallow_repository = MagicMock(return_value=True)

        git_ops.update_existing_repository(
            tmp_path,
            "https://github.com/acme/repository.git",
            branch="main",
        )

        fetch_command = git_ops._run_git_command_with_auth_retry.call_args.args[0]
        assert fetch_command == [
            "git", "-C", str(tmp_path), "fetch", "--depth", "1", "origin", "main",
        ]

    def test_git_timeout_sanitization_accepts_byte_output(self):

        git_ops = GitOperations(github_token="secret-token")
        timeout = subprocess.TimeoutExpired(
            ["git", "clone", "https://github.com/acme/repository"],
            120,
            output=b"clone output\n",
            stderr=b"authentication secret-token failed\n",
        )

        sanitized = git_ops._sanitize_timeout_error(timeout)

        assert sanitized.output == "clone output\n"
        assert sanitized.stderr == "authentication *** failed\n"
        assert git_ops.summarize_exception(sanitized) == (
            "git clone timed out after 120s"
        )

    def test_called_process_error_str_includes_git_stderr(self):
        """净化后的 git 错误 str() 带 stderr 失败原因，traceback 可直接排查。"""

        git_ops = GitOperations(github_token=None)
        exc = git_ops._sanitize_called_process_error(
            subprocess.CalledProcessError(
                128, ["git", "fetch"],
                output="", stderr="OpenSSL SSL_connect: SSL_ERROR_SYSCALL",
            )
        )

        assert isinstance(exc, subprocess.CalledProcessError)
        assert isinstance(exc, GitCommandError)
        assert "SSL_ERROR_SYSCALL" in str(exc)

    def test_called_process_error_str_masks_github_token(self):
        """str() 里的 stderr 同样做 token 打码，不泄露凭据。"""

        git_ops = GitOperations(github_token="secret-token-123")
        exc = git_ops._sanitize_called_process_error(
            subprocess.CalledProcessError(
                128,
                ["git", "fetch", "https://x/secret-token-123"],
                output="",
                stderr="auth failed for secret-token-123",
            )
        )

        assert "secret-token-123" not in str(exc)
        assert "***" in str(exc)

    async def test_skill_scanner_stores_skill_directory_tree_hash_not_repo_head(self, tmp_path):

        repository = tmp_path / "repository"
        repository.mkdir()
        _git(repository, "init")
        _git(repository, "config", "user.email", "tests@example.com")
        _git(repository, "config", "user.name", "WittyHub Tests")

        skill_dir = repository / "skills" / "a"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            f"---\nname: a\ndescription: Tree-hash fixture skill.\n---\n{VALID_SKILL_BODY}\n",
            encoding="utf-8",
        )
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "add skill a")
        skill_dir_tree_hash = _git(repository, "rev-parse", "HEAD:skills/a")

        (repository / "README.md").write_text("# Changed repo head\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "change readme")
        repo_head_commit = _git(repository, "rev-parse", "HEAD")

        skill_repository = MagicMock()
        skill_repository.load_scan_records = AsyncMock(return_value=({}, {}))
        scanner = SkillScanner(
            git_ops=GitOperations(),
            skill_repository=skill_repository,
            category_classifier=None,
        )
        repo = SimpleNamespace(
            id=uuid.uuid4(),
            source="github",
            url="https://github.com/acme/agent-skills",
            branch="master",
            platform=None,
        )

        skills, _ = await scanner.start_scan(
            repo=repo,
            repo_root=repository,
            repository_git_metadata={"commit_id": repo_head_commit},
        )

        assert skills[0].commit_id == repo_head_commit
        assert skills[0].tree_hash == skill_dir_tree_hash

    async def test_skill_scanner_discovers_tag_only_skills_with_different_path(
        self, tmp_path,
    ):
        """Tag paths must be discovered per ref, not filtered from HEAD's set.

        Regression: a repo whose HEAD contains skills/skill-1/SKILL.md while its
        tags only contain skill/SKILL.md used to yield zero SkillVersion records
        because the tag scan reused HEAD's path list.
        """

        repository = tmp_path / "repository"
        repository.mkdir()
        _git(repository, "init")
        _git(repository, "config", "user.email", "tests@example.com")
        _git(repository, "config", "user.name", "WittyHub Tests")

        tagged_dir = repository / "skill"
        tagged_dir.mkdir(parents=True)
        (tagged_dir / "SKILL.md").write_text(
            f"---\nname: tagged-skill\ndescription: Tagged fixture skill.\n---\n{VALID_SKILL_BODY}\n", encoding="utf-8",
        )
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "add tagged skill")
        v1_commit = _git(repository, "rev-parse", "HEAD")
        _git(repository, "tag", "v1")

        (repository / "README.md").write_text("# v2 change\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "unrelated change")
        v2_commit = _git(repository, "rev-parse", "HEAD")
        _git(repository, "tag", "v2")

        head_dir = repository / "skills" / "skill-1"
        head_dir.mkdir(parents=True)
        (head_dir / "SKILL.md").write_text(
            f"---\nname: head-skill\ndescription: Head fixture skill.\n---\n{VALID_SKILL_BODY}\n", encoding="utf-8",
        )
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "add head skill")
        repo_head_commit = _git(repository, "rev-parse", "HEAD")

        skill_repository = MagicMock()
        skill_repository.load_scan_records = AsyncMock(return_value=({}, {}))
        scanner = SkillScanner(
            git_ops=GitOperations(),
            skill_repository=skill_repository,
            category_classifier=None,
        )
        repo = SimpleNamespace(
            id=uuid.uuid4(),
            source="atomgit",
            url="https://atomgit.com/acme/widgets",
            branch="master",
            platform=None,
        )

        skills, tagged_skills = await scanner.start_scan(
            repo=repo,
            repo_root=repository,
            repository_git_metadata={
                "commit_id": repo_head_commit,
                # 与生产扫描一致：按创建时间降序传入。
                # v1/v2 两个 tag 的 skill 目录内容相同（仅 README 变化），
                # tree_hash 去重后只保留最新的 v2 版本行。
                "latest_tags": ["v2", "v1"],
                "latest_tag_commits": {"v1": v1_commit, "v2": v2_commit},
            },
        )

        assert {skill.skill_id for skill in skills} == {
            "atomgit:acme/widgets/skill",
            "atomgit:acme/widgets/skill-1",
        }
        assert {(v.version, v.commit_id) for v in tagged_skills} == {
            ("v2", v2_commit),
        }
        assert {v.skill_id for v in tagged_skills} == {"atomgit:acme/widgets/skill"}

    async def test_skill_scanner_pre_dedups_same_commit_tags(self, tmp_path):
        """Tags pointing at the same commit must not re-run record assembly.

        When two tags share one commit (e.g. v4 and V3), the second tag is
        skipped before _build_skill_record, so security resolution (and its
        Jenkins submission) is not invoked again for the duplicate.
        """

        repository = tmp_path / "repository"
        repository.mkdir()
        _git(repository, "init")
        _git(repository, "config", "user.email", "tests@example.com")
        _git(repository, "config", "user.name", "WittyHub Tests")

        skill_dir = repository / "skills" / "skill-1"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: dup-skill\ndescription: Duplicate fixture skill.\n---\n{VALID_SKILL_BODY}\n", encoding="utf-8",
        )
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "add skill")
        shared_commit = _git(repository, "rev-parse", "HEAD")
        _git(repository, "tag", "v4")
        _git(repository, "tag", "V3")

        skill_repository = MagicMock()
        skill_repository.load_scan_records = AsyncMock(return_value=({}, {}))
        scanner = SkillScanner(
            git_ops=GitOperations(),
            skill_repository=skill_repository,
            category_classifier=None,
        )
        repo = SimpleNamespace(
            id=uuid.uuid4(),
            source="atomgit",
            url="https://atomgit.com/acme/widgets",
            branch="master",
            platform=None,
        )

        skills, tagged_skills = await scanner.start_scan(
            repo=repo,
            repo_root=repository,
            repository_git_metadata={
                "commit_id": shared_commit,
                "latest_tags": ["v4", "V3"],
                "latest_tag_commits": {"v4": shared_commit, "V3": shared_commit},
            },
        )

        # v4 is kept, V3 pointing at the same commit is pre-deduplicated away.
        assert {v.version for v in tagged_skills} == {"v4"}
        assert all(v.commit_id == shared_commit for v in tagged_skills)
        # Security resolution ran once per discovered record, not twice.
        assert scanner._security_cache_hits + scanner._security_audit_submitted == len(
            skills
        ) + len(tagged_skills)
        # Pending audits are a subset of submitted ones.
        assert scanner._security_audit_pending <= scanner._security_audit_submitted

    async def test_skill_scanner_dedups_tags_with_same_skill_tree_hash(self, tmp_path):
        """Tags whose skill directory content is unchanged must collapse to one row.

        Regression (azure-messaging style): a repo cuts v1/v2/v3 tags where only
        README changed — the skill's tree_hash is identical across all tags, so
        only the newest tag may stay in skill_versions; older tags are skipped
        before record assembly.
        """

        repository = tmp_path / "repository"
        repository.mkdir()
        _git(repository, "init")
        _git(repository, "config", "user.email", "tests@example.com")
        _git(repository, "config", "user.name", "WittyHub Tests")

        skill_dir = repository / "skills" / "skill-1"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: stable-skill\ndescription: Stable fixture skill.\n---\n{VALID_SKILL_BODY}\n", encoding="utf-8",
        )
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "add skill")
        v1_commit = _git(repository, "rev-parse", "HEAD")
        _git(repository, "tag", "v1")
        skill_tree_hash = _git(repository, "rev-parse", "HEAD:skills/skill-1")

        (repository / "README.md").write_text("# change 1\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "change readme 1")
        v2_commit = _git(repository, "rev-parse", "HEAD")
        _git(repository, "tag", "v2")

        (repository / "README.md").write_text("# change 2\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "change readme 2")
        v3_commit = _git(repository, "rev-parse", "HEAD")
        _git(repository, "tag", "v3")

        skill_repository = MagicMock()
        skill_repository.load_scan_records = AsyncMock(return_value=({}, {}))
        scanner = SkillScanner(
            git_ops=GitOperations(),
            skill_repository=skill_repository,
            category_classifier=None,
        )
        repo = SimpleNamespace(
            id=uuid.uuid4(),
            source="github",
            url="https://github.com/acme/agent-skills",
            branch="master",
            platform=None,
        )

        skills, tagged_skills = await scanner.start_scan(
            repo=repo,
            repo_root=repository,
            repository_git_metadata={
                "commit_id": v3_commit,
                # 与真实扫描一致：按创建时间降序传入
                "latest_tags": ["v3", "v2", "v1"],
                "latest_tag_commits": {
                    "v1": v1_commit,
                    "v2": v2_commit,
                    "v3": v3_commit,
                },
            },
        )

        # 三个 tag 的 skill 目录内容相同（tree_hash 一致），只保留最新 tag v3。
        assert {v.version for v in tagged_skills} == {"v3"}
        assert all(v.tree_hash == skill_tree_hash for v in tagged_skills)
        # v3 是首个遇到的（最新）tag，记录保留其自身 commit。
        assert tagged_skills[0].commit_id == v3_commit
        # HEAD 的 latest 行内容未变，安全结果经 tree_hash 缓存复用。
        assert scanner._security_cache_hits + scanner._security_audit_submitted == len(
            skills
        ) + len(tagged_skills)

    async def test_skill_scanner_prefers_metadata_version_over_tag(self, tmp_path):
        """SKILL.md frontmatter version wins over the tag name as version.

        Tag v1 declares `version: 1.2.3` in frontmatter → skill_versions row
        uses '1.2.3' (version_source=metadata); tag v2 has no frontmatter
        version → falls back to the tag name 'v2' (version_source=tag).
        """

        repository = tmp_path / "repository"
        repository.mkdir()
        _git(repository, "init")
        _git(repository, "config", "user.email", "tests@example.com")
        _git(repository, "config", "user.name", "WittyHub Tests")

        skill_dir = repository / "skills" / "skill-1"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: versioned-skill\ndescription: Versioned fixture skill.\nversion: 1.2.3\n---\n{VALID_SKILL_BODY}\n",
            encoding="utf-8",
        )
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "add skill with metadata version")
        v1_commit = _git(repository, "rev-parse", "HEAD")
        _git(repository, "tag", "v1")

        (skill_dir / "SKILL.md").write_text(
            f"---\nname: versioned-skill\ndescription: Versioned fixture skill.\n---\n{VALID_SKILL_BODY}\n", encoding="utf-8",
        )
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "drop metadata version")
        v2_commit = _git(repository, "rev-parse", "HEAD")
        _git(repository, "tag", "v2")

        skill_repository = MagicMock()
        skill_repository.load_scan_records = AsyncMock(return_value=({}, {}))
        scanner = SkillScanner(
            git_ops=GitOperations(),
            skill_repository=skill_repository,
            category_classifier=None,
        )
        repo = SimpleNamespace(
            id=uuid.uuid4(),
            source="github",
            url="https://github.com/acme/agent-skills",
            branch="master",
            platform=None,
        )

        skills, tagged_skills = await scanner.start_scan(
            repo=repo,
            repo_root=repository,
            repository_git_metadata={
                "commit_id": v2_commit,
                "latest_tags": ["v2", "v1"],
                "latest_tag_commits": {"v1": v1_commit, "v2": v2_commit},
            },
        )

        versions = {v.version: v for v in tagged_skills}
        # frontmatter 声明的 version 优先于 tag 名
        assert "1.2.3" in versions
        assert versions["1.2.3"].commit_id == v1_commit
        assert versions["1.2.3"].extra_metadata["version_source"] == "metadata"
        # 元数据缺失 version 时回退 tag 名
        assert "v2" in versions
        assert versions["v2"].commit_id == v2_commit
        assert versions["v2"].extra_metadata["version_source"] == "tag"
        # tag 名不再进入版本号
        assert "v1" not in versions

    async def test_skill_scanner_skips_tags_when_head_declares_version(self, tmp_path):
        """HEAD SKILL.md 声明 version 的 skill 完全跳过 tag 版本收集。

        用户场景（weijihui/test 仓库）：skill-3 在 HEAD 声明 version: 1.0.1，
        但 tag v2.0.0/v3.0.0 处的 SKILL.md 没有 version → 该 skill 只产出
        一条 metadata 版本行（1.0.1），不再产出任何 tag 版本行；
        未声明 version 的 skill-1 仍正常按 tag 名收集版本。
        """

        repository = tmp_path / "repository"
        repository.mkdir()
        _git(repository, "init")
        _git(repository, "config", "user.email", "tests@example.com")
        _git(repository, "config", "user.name", "WittyHub Tests")

        # commit 1: 仅 skill-1（无 version）
        skill1_dir = repository / "skills" / "skill-1"
        skill1_dir.mkdir(parents=True)
        (skill1_dir / "SKILL.md").write_text(
            f"---\nname: plain-skill\ndescription: Plain fixture skill.\n---\n{VALID_SKILL_BODY}\n", encoding="utf-8",
        )
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "add skill-1")
        v1_commit = _git(repository, "rev-parse", "HEAD")
        _git(repository, "tag", "v1.0.0")

        # commit 2: 新增 skill-3（无 version）
        skill3_dir = repository / "skills" / "skill-3"
        skill3_dir.mkdir(parents=True)
        (skill3_dir / "SKILL.md").write_text(
            f"---\nname: self-versioned-skill\ndescription: Self-versioned fixture skill.\n---\n{VALID_SKILL_BODY}\n",
            encoding="utf-8",
        )
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "add skill-3 without version")
        v2_commit = _git(repository, "rev-parse", "HEAD")
        _git(repository, "tag", "v2.0.0")

        # commit 3 (HEAD): skill-3 声明 version: 1.0.1
        (skill3_dir / "SKILL.md").write_text(
            f"---\nname: self-versioned-skill\ndescription: Self-versioned fixture skill.\nversion: 1.0.1\n---\n{VALID_SKILL_BODY}\n",
            encoding="utf-8",
        )
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "declare metadata version")
        head_commit = _git(repository, "rev-parse", "HEAD")
        _git(repository, "tag", "v3.0.0")

        skill_repository = MagicMock()
        skill_repository.load_scan_records = AsyncMock(return_value=({}, {}))
        scanner = SkillScanner(
            git_ops=GitOperations(),
            skill_repository=skill_repository,
            category_classifier=None,
        )
        repo = SimpleNamespace(
            id=uuid.uuid4(),
            source="atomgit",
            url="https://atomgit.com/weijihui/test",
            branch="main",
            platform=None,
        )

        skills, tagged_skills = await scanner.start_scan(
            repo=repo,
            repo_root=repository,
            repository_git_metadata={
                "commit_id": head_commit,
                "latest_tags": ["v3.0.0", "v2.0.0", "v1.0.0"],
                "latest_tag_commits": {
                    "v1.0.0": v1_commit,
                    "v2.0.0": v2_commit,
                    "v3.0.0": head_commit,
                },
            },
        )

        by_skill: dict[str, list] = {}
        for v in tagged_skills:
            by_skill.setdefault(v.skill_id, []).append(v)

        skill1_id = next(s.skill_id for s in skills if s.name == "plain-skill")
        skill3_id = next(
            s.skill_id for s in skills if s.name == "self-versioned-skill"
        )

        # skill-3：仅一条 metadata 版本行，无任何 tag 版本行
        skill3_versions = by_skill.get(skill3_id, [])
        assert len(skill3_versions) == 1
        assert skill3_versions[0].version == "1.0.1"
        assert skill3_versions[0].commit_id == head_commit
        assert (
            skill3_versions[0].extra_metadata["version_source"] == "metadata"
        )

        # skill-1：无声明，仍按 tag 名收集（tree_hash 一致去重到最新 tag）
        skill1_versions = by_skill.get(skill1_id, [])
        assert {v.version for v in skill1_versions} == {"v3.0.0"}

    async def test_skill_scanner_uses_declared_category_when_valid(self, tmp_path):
        """frontmatter 声明了合法 category（在 CANONICAL_CATEGORIES 中）→ 直接使用，
        跳过分类器。"""

        repository = tmp_path / "repository"
        repository.mkdir()
        _git(repository, "init")
        _git(repository, "config", "user.email", "tests@example.com")
        _git(repository, "config", "user.name", "WittyHub Tests")

        skill_dir = repository / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: my-skill\n"
            "description: Category-cache fixture skill.\n"
            "category: Engineering and Compilation\n"
            f"---\n{VALID_SKILL_BODY}\n",
            encoding="utf-8",
        )
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "add skill")
        head_commit = _git(repository, "rev-parse", "HEAD")

        # 分类器被 mock，如果被调用会返回错误值
        classifier = MagicMock()
        classifier.classify = MagicMock(return_value="Security Hardening")

        skill_repository = MagicMock()
        skill_repository.load_scan_records = AsyncMock(return_value=({}, {}))
        scanner = SkillScanner(
            git_ops=GitOperations(),
            skill_repository=skill_repository,
            category_classifier=classifier,
        )
        repo = SimpleNamespace(
            id=uuid.uuid4(),
            source="github",
            url="https://github.com/acme/repo",
            branch="master",
            platform=None,
        )

        skills, _ = await scanner.start_scan(
            repo=repo,
            repo_root=repository,
            repository_git_metadata={"commit_id": head_commit},
        )

        # 合法声明 → 直接使用，分类器未被调用
        classifier.classify.assert_not_called()
        assert skills[0].category == "Engineering and Compilation"

    async def test_skill_scanner_falls_back_to_classifier_when_category_invalid(
        self, tmp_path,
    ):
        """frontmatter 声明了非法 category（不在 CANONICAL_CATEGORIES 中）→
        回退到分类器。"""

        repository = tmp_path / "repository"
        repository.mkdir()
        _git(repository, "init")
        _git(repository, "config", "user.email", "tests@example.com")
        _git(repository, "config", "user.name", "WittyHub Tests")

        skill_dir = repository / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: my-skill\n"
            "description: Category-cache fixture skill.\n"
            "category: Some Random Category\n"
            f"---\n{VALID_SKILL_BODY}\n",
            encoding="utf-8",
        )
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "add skill")
        head_commit = _git(repository, "rev-parse", "HEAD")

        # 分类器返回正确分类
        classifier = MagicMock()
        classifier.classify = MagicMock(return_value="Research and Design")

        skill_repository = MagicMock()
        skill_repository.load_scan_records = AsyncMock(return_value=({}, {}))
        scanner = SkillScanner(
            git_ops=GitOperations(),
            skill_repository=skill_repository,
            category_classifier=classifier,
        )
        repo = SimpleNamespace(
            id=uuid.uuid4(),
            source="github",
            url="https://github.com/acme/repo",
            branch="master",
            platform=None,
        )

        skills, _ = await scanner.start_scan(
            repo=repo,
            repo_root=repository,
            repository_git_metadata={"commit_id": head_commit},
        )

        # 非法声明 → 分类器被调用，使用分类器结果
        classifier.classify.assert_called_once()
        assert skills[0].category == "Research and Design"

    def test_skill_scanner_reuses_security_result_for_unchanged_skill_tree(self, tmp_path):

        repository = tmp_path / "repository"
        repository.mkdir()
        _git(repository, "init")
        _git(repository, "config", "user.email", "tests@example.com")
        _git(repository, "config", "user.name", "WittyHub Tests")

        skill_dir = repository / "skills" / "production-skill"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            f"---\nname: example\ndescription: Example fixture skill.\n---\n{VALID_SKILL_BODY}\n", encoding="utf-8",
        )
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "add skill")
        skill_commit = _git(repository, "rev-parse", "HEAD")
        skill_tree_hash = _git(repository, "rev-parse", "HEAD:skills/production-skill")

        (repository / "README.md").write_text("# unrelated change\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "change readme")
        repo_head = _git(repository, "rev-parse", "HEAD")

        repo = SimpleNamespace(
            id=uuid.uuid4(),
            source="github",
            url="https://github.com/acme/repository",
            branch="master",
            platform=None,
        )
        skill_id = build_skill_id(
            repo.source, extract_owner_repo(repo.url), "skills/production-skill/SKILL.md",
        )
        existing = SimpleNamespace(
            commit_id=skill_commit,
            tree_hash=skill_tree_hash,
            risk_score=12,
            extra_metadata={
                "security_audit": {"skillspector_score": 12},
            },
        )
        skill_repository = MagicMock()
        skill_repository.load_scan_records = AsyncMock(
            return_value=({skill_id: existing}, {})
        )
        scanner = SkillScanner(
            git_ops=GitOperations(),
            skill_repository=skill_repository,
            category_classifier=None,
        )
        scanner._audit_skill_security = AsyncMock()

        skills, _ = asyncio.run(
            scanner.start_scan(
                repo=repo,
                repo_root=repository,
                repository_git_metadata={"commit_id": repo_head},
            )
        )

        scanner._audit_skill_security.assert_not_awaited()
        assert skills[0].skill_id == skill_id
        assert skills[0].commit_id == repo_head
        assert skills[0].tree_hash == skill_tree_hash
        assert skills[0].risk_score == 12
        assert getattr(skills[0], "_security_audit_triggered") is False

    async def test_skill_scanner_requires_name_and_description(self, tmp_path):
        """可扫描 Skill 门禁：frontmatter 必须同时有 name 与 description。"""

        repository = tmp_path / "repository"
        repository.mkdir()
        _git(repository, "init")
        _git(repository, "config", "user.email", "tests@example.com")
        _git(repository, "config", "user.name", "WittyHub Tests")

        fixtures = {
            "with-both": (
                f"---\nname: with-both\ndescription: A complete skill.\n---\n{VALID_SKILL_BODY}\n"
            ),
            "no-description": f"---\nname: no-description\n---\n{VALID_SKILL_BODY}\n",
            "no-name": f"---\ndescription: Only description.\n---\n{VALID_SKILL_BODY}\n",
            "empty-description": (
                f"---\nname: empty-description\ndescription: ''\n---\n{VALID_SKILL_BODY}\n"
            ),
        }
        for name, text in fixtures.items():
            skill_dir = repository / "skills" / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "add skills")
        head_commit = _git(repository, "rev-parse", "HEAD")

        skill_repository = MagicMock()
        skill_repository.load_scan_records = AsyncMock(return_value=({}, {}))
        scanner = SkillScanner(
            git_ops=GitOperations(),
            skill_repository=skill_repository,
            category_classifier=None,
        )
        repo = SimpleNamespace(
            id=uuid.uuid4(),
            source="atomgit",
            url="https://atomgit.com/acme/widgets",
            branch="master",
            platform=None,
        )

        skills, _ = await scanner.start_scan(
            repo=repo,
            repo_root=repository,
            repository_git_metadata={"commit_id": head_commit},
        )

        assert {skill.skill_id for skill in skills} == {"atomgit:acme/widgets/with-both"}

    async def test_configured_discover_force_does_not_skip_commit_unchanged_check(self):
        skill_repository = MagicMock()
        skill_repository.list_unscored_by_skill_repo = AsyncMock(return_value=([], []))
        manager = SkillManager(skill_repository, MagicMock())
        discover_mock = AsyncMock()
        with (
            patch.object(SkillManager, "_sync_git_repository"),
            patch.object(SkillManager, "_list_skill_paths", return_value=["SKILL.md"]),
            patch.object(SkillManager, "_is_commit_unchanged", return_value=True),
            patch.object(
                SkillManager,
                "_get_or_create_skill_repository",
                AsyncMock(
                    return_value=(
                        SimpleNamespace(
                            id=uuid.uuid4(),
                            repo_name="acme_agent-skills",
                            source="github",
                            platform=None,
                        ),
                        False,
                    )
                ),
            ),
            patch.object(SkillManager, "_discover_and_store_skills", discover_mock),
        ):
            result = await manager.discover_configured_skill_repository(
                SkillRepositoryRequest(url="https://github.com/acme/agent-skills")
            )

        assert getattr(result, "_unchanged") is True
        discover_mock.assert_not_called()

    async def test_commit_unchanged_skips_rescan_without_inline_security_retry(self):
        """commit 未变只标记 _unchanged：安全补审计由 remediation 全局兜底。"""

        manager = SkillManager(MagicMock(), MagicMock())
        discover_mock = AsyncMock()
        repository = SimpleNamespace(
            id=uuid.uuid4(),
            repo_name="acme_agent-skills",
            source="github",
            platform=None,
        )
        with (
            patch.object(SkillManager, "_sync_git_repository"),
            patch.object(SkillManager, "_list_skill_paths", return_value=["SKILL.md"]),
            patch.object(SkillManager, "_is_commit_unchanged", return_value=True),
            patch.object(
                SkillManager,
                "_get_or_create_skill_repository",
                AsyncMock(return_value=(repository, False)),
            ),
            patch.object(SkillManager, "_discover_and_store_skills", discover_mock),
        ):
            result = await manager.discover_configured_skill_repository(
                SkillRepositoryRequest(url="https://github.com/acme/agent-skills")
            )

        assert getattr(result, "_unchanged") is True
        assert not hasattr(result, "_security_retry_candidates")
        discover_mock.assert_not_called()

    async def test_sync_catalog_source_migrates_platform_for_existing_repo(self):
        """Issue #9: a directly-collected repo (platform empty) registered in the
        anchor catalog must migrate skill_repos / skills platform to community."""

        session = MagicMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        skill_repository = MagicMock()
        skill_repository.session = session
        repo_repository = MagicMock()
        repo_repository.update_skill_repository = AsyncMock(
            return_value=SimpleNamespace(id=1)
        )
        repo_repository.get_skill_repository_by_id = AsyncMock(
            return_value=SimpleNamespace(id=1)
        )
        manager = SkillManager(skill_repository, repo_repository)

        repository = SimpleNamespace(
            id=1,
            repo_name="openeuler_demo-skills",
            source="atomgit",
            platform="",
        )

        returned = await manager._sync_catalog_source(
            repository,
            SkillRepositoryRequest(
                url="https://atomgit.com/openeuler/demo-skills",
                platform="community",
            ),
        )

        assert returned is not None
        repo_repository.update_skill_repository.assert_awaited_once()
        kwargs = repo_repository.update_skill_repository.await_args.kwargs
        assert kwargs["platform"] == "community"
        assert kwargs["commit"] is False
        # skills + skill_versions rows are migrated to the catalog platform
        executed = [call.args[0] for call in session.execute.await_args_list]
        assert len(executed) == 2
        for stmt in executed:
            assert isinstance(stmt, Update)
        assert session.commit.await_count == 1

        migrated = executed[0].compile(dialect=postgresql.dialect()).params
        assert migrated["platform"] == "community"

    async def test_delete_removed_catalog_repos_deletes_moved_community_author(self):
        """community 仓库从旧 SIG 挪到新 SIG：数据库记录的 (platform, author,
        repo_name) 组合在 catalog 期望集合中已不存在，按交集删除旧归属，
        供后续扫描重建到新归属。"""

        session = MagicMock()
        skill_repository = MagicMock()
        skill_repository.session = session
        repo_repository = MagicMock()
        repo_repository.delete_skill_repositories = AsyncMock()
        repo_repository.reconcile_skill_nums = AsyncMock(return_value=0)
        contributor_repository = MagicMock()
        contributor_repository.reconcile_skill_counts = AsyncMock(return_value=0)
        contributor_repository.delete_contributors_without_skills = AsyncMock(return_value=0)
        manager = SkillManager(skill_repository, repo_repository, contributor_repository)

        moved_request = SkillRepositoryRequest(
            url="https://atomgit.com/openeuler/agent-skills",
            platform="community",
            sig_name="Infrastructure",
        )
        stayed_request = SkillRepositoryRequest(
            url="https://atomgit.com/openeuler/other-skills",
            platform="community",
            sig_name="Infrastructure",
        )
        moved_repo_name = manager._derive_repo_name(
            manager._normalize_repository_request(moved_request)
        )
        stayed_repo_name = manager._derive_repo_name(
            manager._normalize_repository_request(stayed_request)
        )

        repo_repository.list_skill_repositories = AsyncMock(
            return_value=[
                SimpleNamespace(
                    id=1,
                    repo_name=moved_repo_name,
                    platform="community",
                    author="unassigned-sig",
                ),
                SimpleNamespace(
                    id=2,
                    repo_name=stayed_repo_name,
                    platform="community",
                    author="Infrastructure",
                ),
            ]
        )

        deleted = await manager.delete_removed_catalog_repos(
            [moved_request, stayed_request]
        )

        assert deleted == 1
        repo_repository.delete_skill_repositories.assert_awaited_once()
        removed_ids = repo_repository.delete_skill_repositories.await_args.args[0]
        assert removed_ids == [1]

    async def test_delete_removed_catalog_repos_uses_url_owner_for_non_community(self):
        """非 community 仓库的 (author, repo_name) 中 author 取 URL owner，
        URL 未变则归属未变，不会被误删。"""

        session = MagicMock()
        skill_repository = MagicMock()
        skill_repository.session = session
        repo_repository = MagicMock()
        repo_repository.delete_skill_repositories = AsyncMock()
        repo_repository.reconcile_skill_nums = AsyncMock(return_value=0)
        contributor_repository = MagicMock()
        contributor_repository.reconcile_skill_counts = AsyncMock(return_value=0)
        contributor_repository.delete_contributors_without_skills = AsyncMock(return_value=0)
        manager = SkillManager(skill_repository, repo_repository, contributor_repository)

        request = SkillRepositoryRequest(
            url="https://github.com/acme/agent-skills",
            platform="enterprise",
        )
        normalized = manager._normalize_repository_request(request)
        repo_name = manager._derive_repo_name(normalized)
        assert manager._derive_catalog_author(normalized) == "acme"

        repo_repository.list_skill_repositories = AsyncMock(
            return_value=[
                SimpleNamespace(
                    id=1,
                    repo_name=repo_name,
                    platform="enterprise",
                    author="acme",
                ),
            ]
        )

        deleted = await manager.delete_removed_catalog_repos([request])

        assert deleted == 0
        repo_repository.delete_skill_repositories.assert_not_called()

    async def test_sync_catalog_source_skips_when_platform_unchanged(self):
        """Issue #9: no migration (and no writes) when the catalog view matches."""

        session = MagicMock()
        session.execute = AsyncMock()
        skill_repository = MagicMock()
        skill_repository.session = session
        repo_repository = MagicMock()
        manager = SkillManager(skill_repository, repo_repository)

        repository = SimpleNamespace(
            id=1,
            repo_name="openeuler_demo-skills",
            source="atomgit",
            platform="community",
        )

        returned = await manager._sync_catalog_source(
            repository,
            SkillRepositoryRequest(
                url="https://atomgit.com/openeuler/demo-skills",
                platform="community",
            ),
        )

        assert returned is repository
        repo_repository.update_skill_repository.assert_not_called()
        session.execute.assert_not_called()
        session.commit.assert_not_called()

    async def test_remediation_trigger_chunk_builds_audit_details(self):
        """补审计触发：命中 Jenkins 后返回 (行, details)；无 build number 计 failed。"""

        row = security_backfill._UnscoredRow(
            table="skills",
            id=uuid.uuid4(),
            skill_id="github/acme/agent-skills/skills/example",
            source_url=(
                "https://github.com/acme/agent-skills/"
                "blob/main/skills/example/SKILL.md"
            ),
            version="latest",
            commit_id="a" * 40,
            repo_url="https://github.com/acme/agent-skills",
            repo_branch="main",
            repo_source="github",
            repo_local_path=None,
        )
        stats = security_backfill.SecurityBackfillStats(total=1)

        with patch(
            "src.ai.security_backfill.trigger_existing_skill_audit",
            AsyncMock(
                return_value=SimpleNamespace(
                    details={
                        "skillspector_async": True,
                        "skillspector_build_number": 123,
                        "source": "skillspector",
                    }
                )
            ),
        ) as trigger_mock:
            triggered = await security_backfill._trigger_chunk(
                [row], MagicMock(), MagicMock(), stats
            )

        assert [(r.id, d["skillspector_build_number"]) for r, d in triggered] == [
            (row.id, 123)
        ]
        assert stats.failed == 0
        # 触发参数：相对路径由 source_url 解析，与扫描路径同口径
        call = trigger_mock.await_args.kwargs
        assert call["relative_path"] == "skills/example/SKILL.md"
        assert call["skill_id"] == row.skill_id
        assert call["security_async_mode"] is True

    async def test_remediation_trigger_chunk_counts_failed_trigger(self):
        """触发失败（无 build number）不写库，只计 failed 留待下轮重试。"""

        row = security_backfill._UnscoredRow(
            table="skill_versions",
            id=uuid.uuid4(),
            skill_id="github/acme/agent-skills/example",
            source_url="https://github.com/acme/agent-skills/blob/main/SKILL.md",
            version=None,
            commit_id=None,
            repo_url="https://github.com/acme/agent-skills",
            repo_branch=None,
            repo_source="github",
            repo_local_path=None,
        )
        stats = security_backfill.SecurityBackfillStats(total=1)

        with patch(
            "src.ai.security_backfill.trigger_existing_skill_audit",
            AsyncMock(
                return_value=SimpleNamespace(
                    details={
                        "skillspector_async": True,
                        "skillspector_build_number": None,
                        "source": "skillspector",
                    }
                )
            ),
        ):
            triggered = await security_backfill._trigger_chunk(
                [row], MagicMock(), MagicMock(), stats
            )

        assert triggered == []
        assert stats.failed == 1

    async def test_discover_store_uses_one_final_repository_commit(self, tmp_path):
        skill_repository = MagicMock()
        skill_repository.store_skills_and_versions = AsyncMock()
        repo_repository = MagicMock()
        updated_repo = SimpleNamespace(id=uuid.uuid4())
        repo_repository.update_skill_repository = AsyncMock(return_value=updated_repo)
        manager = SkillManager(skill_repository, repo_repository)
        repo = SimpleNamespace(
            id=updated_repo.id,
            platform=None,
            repo_name="github.com_acme_skills",
        )

        with (
            patch.object(
                SkillManager,
                "_discover_skills",
                AsyncMock(return_value=([], [], "c" * 40)),
            ),
            patch.object(
                SkillManager,
                "_store_to_security_audits",
                AsyncMock(),
            ),
        ):
            result = await manager._discover_and_store_skills(
                repo,
                clone_dir=tmp_path,
                repo_name="github.com_acme_skills",
            )

        skill_repository.store_skills_and_versions.assert_awaited_once_with(
            repo.id,
            [],
            [],
            commit=False,
        )
        repo_repository.update_skill_repository.assert_awaited_once_with(
            repo.id,
            repository_commit_id="c" * 40,
            skill_discover_status="done",
            skill_num=0,
            author=None,
        )
        assert result is updated_repo

    async def test_discover_store_upserts_contributor_with_catalog_profile(self, tmp_path):
        """enterprise/personal 仓库 author 未显式传入时用扫描结果兜底，
        且 upsert contributors 时注入 catalog 的 name/description/git_profile。"""

        skill_repository = MagicMock()
        skill_repository.store_skills_and_versions = AsyncMock()
        repo_repository = MagicMock()
        updated_repo = SimpleNamespace(id=uuid.uuid4())
        repo_repository.update_skill_repository = AsyncMock(return_value=updated_repo)
        contributor_repository = MagicMock()
        contributor_repository.upsert = AsyncMock()
        # skill_count 从 skills 表聚合（含同 author 其他仓库），而非单仓库扫描数
        contributor_repository.count_skills = AsyncMock(return_value=7)
        manager = SkillManager(
            skill_repository,
            repo_repository,
            contributor_repository=contributor_repository,
            catalog_path=Path("/nonexistent-catalog"),
        )
        repo = SimpleNamespace(
            id=updated_repo.id,
            platform="enterprise",
            repo_name="github.com_wordpress_agent-skills",
            source="github",
            url="https://github.com/wordpress/agent-skills",
        )
        scanned_skill = SimpleNamespace(skill_id="github/wordpress/agent-skills/x", author="wordpress")

        with (
            patch.object(
                SkillManager,
                "_discover_skills",
                AsyncMock(return_value=([scanned_skill], [], "c" * 40)),
            ),
            patch.object(
                SkillManager,
                "_store_to_security_audits",
                AsyncMock(),
            ),
        ):
            await manager._discover_and_store_skills(
                repo,
                clone_dir=tmp_path,
                repo_name="github.com_wordpress_agent-skills",
            )

        # author 从扫描结果兜底为 URL 推导的 wordpress
        contributor_repository.count_skills.assert_awaited_once_with("github", "wordpress")
        contributor_repository.upsert.assert_awaited_once_with(
            source="github",
            author="wordpress",
            platform="enterprise",
            repo_url="https://github.com/wordpress/agent-skills",
            skill_count=7,
            name=None,
            description=None,
            git_profile=None,
            website=None,
            commit=False,
        )
        # catalog 不可用时 profile 查不到 → name/description/git_profile/website 为 None；
        # 但 skill_repos.author 回写仍生效
        repo_repository.update_skill_repository.assert_awaited_once_with(
            repo.id,
            repository_commit_id="c" * 40,
            skill_discover_status="done",
            skill_num=1,
            author="wordpress",
        )

    async def test_security_audit_store_uses_runtime_trigger_flag(self):
        skill_repository = MagicMock()
        skill_repository.session = MagicMock()
        manager = SkillManager(skill_repository, MagicMock())
        audit_repository = MagicMock()
        audit_repository.upsert_many_by_resource = AsyncMock()
        details = {
            "skillspector_async": True,
            "skillspector_build_number": 123,
        }
        reused = SimpleNamespace(
            id=uuid.uuid4(),
            skill_id="reused",
            version="latest",
            commit_id="a" * 40,
            extra_metadata={"security_audit": details},
            _security_audit_triggered=False,
            _security_audit_attempted=False,
        )
        triggered = SimpleNamespace(
            id=uuid.uuid4(),
            skill_id="triggered",
            version="latest",
            commit_id="b" * 40,
            extra_metadata={"security_audit": details},
            _security_audit_triggered=True,
            _security_audit_attempted=True,
        )

        with patch(
            "src.skills.skill_manager.SecurityAuditRepository",
            return_value=audit_repository,
        ):
            await manager._store_to_security_audits(
                [reused, triggered],
                [],
            )

        audit_repository.upsert_many_by_resource.assert_awaited_once()
        resource_type, entries = audit_repository.upsert_many_by_resource.call_args.args
        assert resource_type == "skill"
        assert len(entries) == 1
        resource_id, audit_data = entries[0]
        assert resource_id == triggered.id
        assert audit_data["resource_id"] == triggered.id
        assert audit_data["commit_id"] == "b" * 40
        assert audit_data["details"] == details

    async def test_security_audit_store_persists_failed_trigger_as_unknown(self):
        """触发失败（build_number=None）时也落 risk_level=unknown 审计记录。"""

        skill_repository = MagicMock()
        skill_repository.session = MagicMock()
        manager = SkillManager(skill_repository, MagicMock())
        audit_repository = MagicMock()
        audit_repository.upsert_many_by_resource = AsyncMock()
        failed_details = {
            "skillspector_async": True,
            "skillspector_build_number": None,
            "source": "skillspector",
        }
        failed = SimpleNamespace(
            id=uuid.uuid4(),
            skill_id="failed",
            version="latest",
            commit_id="c" * 40,
            extra_metadata={"security_audit": failed_details},
            _security_audit_triggered=False,
            _security_audit_attempted=True,
        )
        untouched = SimpleNamespace(
            id=uuid.uuid4(),
            skill_id="untouched",
            version="latest",
            commit_id="d" * 40,
            extra_metadata={},
            _security_audit_triggered=False,
            _security_audit_attempted=False,
        )

        with patch(
            "src.skills.skill_manager.SecurityAuditRepository",
            return_value=audit_repository,
        ):
            await manager._store_to_security_audits(
                [failed, untouched],
                [],
            )

        audit_repository.upsert_many_by_resource.assert_awaited_once()
        resource_type, entries = audit_repository.upsert_many_by_resource.call_args.args
        assert resource_type == "skill"
        assert len(entries) == 1
        resource_id, audit_data = entries[0]
        assert resource_id == failed.id
        assert audit_data["risk_level"] == "unknown"
        assert audit_data["audit_type"] == "skillspector"
        assert audit_data["details"] == failed_details
        assert audit_data["details"]["skillspector_build_number"] is None

    def test_security_retry_resolves_path_from_commit_source_url(self):
        commit_id = "bdf484685eb2a3e13eb223cbf52adf957a98ffb6"
        repository = SimpleNamespace(
            source="github",
            url="https://github.com/github/gh-aw",
            branch="main",
        )
        record = SimpleNamespace(
            commit_id=commit_id,
            source_url=(
                "https://github.com/github/gh-aw/blob/"
                f"{commit_id}/.github/skills/agentic-workflows/SKILL.md"
            ),
            skill_id="github/github/gh-aw/agentic-workflows",
        )

        assert resolve_relative_skill_path(repository, record) == (
            ".github/skills/agentic-workflows/SKILL.md"
        )

    def test_resolve_relative_skill_path_version_tag_with_v_prefix(self):
        """URL 里的 tag 名带 v 前缀（v0.0.3）而 version 字段不带（0.0.3）时仍能解析。

        复现 aspire-orchestration 的 Jenkins exit 3：ref 候选全不匹配后落入
        skill_id 反推兜底，丢掉 skills/ 前缀得到错误路径。
        """
        repository = SimpleNamespace(
            source="github",
            url="https://github.com/microsoft/aspire-skills",
            branch="main",
        )
        record = SimpleNamespace(
            commit_id="a83ca78b9d35b8238f9c4fdba72cb779defe2a90",
            version="0.0.3",
            source_url=(
                "https://github.com/microsoft/aspire-skills/blob/v0.0.3/"
                "skills/aspire-orchestration/SKILL.md"
            ),
            skill_id="github:microsoft/aspire-skills/aspire-orchestration",
        )

        assert resolve_relative_skill_path(repository, record) == (
            "skills/aspire-orchestration/SKILL.md"
        )

    def test_resolve_relative_skill_path_unknown_ref_falls_back_to_url_regex(self):
        """URL 的 ref 不在已知候选里（如版本 tag 名）→ 通用正则提取，保留完整路径。"""
        repository = SimpleNamespace(
            source="github",
            url="https://github.com/microsoft/aspire-skills",
            branch="main",
        )
        record = SimpleNamespace(
            commit_id=None,
            version=None,
            source_url=(
                "https://github.com/microsoft/aspire-skills/blob/v1.0.0-pre-migration/"
                "skills/aspire-orchestration/SKILL.md"
            ),
            skill_id="github:microsoft/aspire-skills/aspire-orchestration",
        )

        assert resolve_relative_skill_path(repository, record) == (
            "skills/aspire-orchestration/SKILL.md"
        )

    def test_skill_manager_commit_unchanged_uses_skill_repo_commit_field(self, tmp_path):
        manager = SkillManager(MagicMock(), MagicMock())
        manager._git_ops = MagicMock()
        manager._git_ops.get_repository_head_commit_id.return_value = "repo-head"

        repository = SimpleNamespace(repository_commit_id="repo-head")

        assert manager._is_commit_unchanged(repository, tmp_path) is True

        repository.repository_commit_id = "older-head"
        assert manager._is_commit_unchanged(repository, tmp_path) is False

    def test_select_skill_by_version_returns_only_match(self):
        repo = SkillRepository(MagicMock())
        skills = [SimpleNamespace(version="v1.0.0")]

        selected = repo._select_skill_by_version(skills, "v9.9.9")

        assert selected is skills[0]

    def test_select_skill_by_version_prefers_requested_version(self):
        repo = SkillRepository(MagicMock())
        latest = SimpleNamespace(version="latest")
        requested = SimpleNamespace(version="v1.2.3")
        skills = [latest, requested]

        selected = repo._select_skill_by_version(skills, "v1.2.3")

        assert selected is requested

    def test_select_skill_by_version_defaults_to_latest(self):
        repo = SkillRepository(MagicMock())
        latest = SimpleNamespace(version="latest")
        older = SimpleNamespace(version="v1.2.3")
        skills = [latest, older]

        selected = repo._select_skill_by_version(skills)

        assert selected is latest

    def test_select_skill_by_version_falls_back_to_original_logic_when_missing(self):
        repo = SkillRepository(MagicMock())
        latest = SimpleNamespace(version="latest")
        older = SimpleNamespace(version="v1.2.3")
        skills = [latest, older]

        selected = repo._select_skill_by_version(skills, "v9.9.9")

        assert selected is latest

    async def test_create_skill_data_validation(self):
        skill_data = {
            "skill_id": "test/skill:v1.0.0",
            "name": "test-skill",
            "description": "Test skill",
            "version": "v1.0.0",
            "commit_id": "abc123",
            "author": "test",
            "source": "clawhub",
            "source_url": "https://example.com/test",
            "category": "Testing",
            "tags": ["test"],
            "platform": "openclaw",
        }
        skill = SkillCreate(**skill_data)
        assert skill.skill_id == "test/skill:v1.0.0"
        assert skill.name == "test-skill"

    async def test_skill_create_missing_required_fields(self):

        with pytest.raises(ValidationError):
            SkillCreate(
                name="test",
                source="clawhub",
                source_url="https://example.com",
            )

    async def test_skill_response_model(self):
        skill_dict = {
            "id": str(uuid.uuid4()),
            "skill_id": "test/skill:v1.0.0",
            "name": "test-skill",
            "description": "Test skill",
            "version": "v1.0.0",
            "commit_id": "abc123",
            "author": "test",
            "source": "clawhub",
            "source_url": "https://example.com/test",
            "category": "Testing",
            "tags": ["test"],
            "platform": "openclaw",
            "extra_metadata": {},
            "risk_score": 85,
            "download_count": 10,
            "rating": "4.5",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        response = SkillResponse(**skill_dict)
        assert response.skill_id == "test/skill:v1.0.0"
        assert response.name == "test-skill"


class TestReconcileSkillStatistics:
    """扫描落库后的统计自愈：contributors.skill_count / skill_repos.skill_num。"""

    def _make_manager(self, repo_repository, contributor_repository):
        return SkillManager(MagicMock(), repo_repository, contributor_repository)

    async def test_reconcile_calls_both_repositories(self):
        contributor_repo = AsyncMock()
        contributor_repo.reconcile_skill_counts.return_value = 2
        repo_repo = AsyncMock()
        repo_repo.reconcile_skill_nums.return_value = 1
        manager = self._make_manager(repo_repo, contributor_repo)

        await manager._reconcile_skill_statistics()

        contributor_repo.reconcile_skill_counts.assert_awaited_once()
        repo_repo.reconcile_skill_nums.assert_awaited_once()

    async def test_reconcile_skips_contributors_when_repository_missing(self):
        repo_repo = AsyncMock()
        manager = self._make_manager(repo_repo, None)

        await manager._reconcile_skill_statistics()

        repo_repo.reconcile_skill_nums.assert_awaited_once()

    async def test_reconcile_swallows_errors(self):
        repo_repo = AsyncMock()
        repo_repo.reconcile_skill_nums.side_effect = RuntimeError("boom")
        manager = self._make_manager(repo_repo, None)

        # 不应抛异常：扫描结果已提交，统计自愈失败只告警
        await manager._reconcile_skill_statistics()


class TestExtractDeclaredVersion:
    """frontmatter version 声明位置兼容：顶层 version 与 metadata.version 嵌套。"""

    def test_top_level_version(self):
        meta, _ = parse_skill_frontmatter_text(
            "---\nname: s\nversion: 1.0.0\n---\nbody\n"
        )
        assert extract_declared_version(meta) == "1.0.0"

    def test_nested_metadata_version_azure_skills_style(self):
        """azure-skills 真实格式：version 嵌套在 metadata: 块下。"""

        text = (
            "---\n"
            'name: airunway-aks-setup\n'
            'description: "Set up AI Runway on AKS."\n'
            "license: MIT\n"
            "metadata:\n"
            "  author: Microsoft\n"
            '  version: "1.1.1"\n'
            "---\n"
            "# AI Runway AKS Setup\n"
        )
        meta, _ = parse_skill_frontmatter_text(text)
        assert extract_declared_version(meta) == "1.1.1"

    def test_nested_metadata_version_as_dict(self):
        """metadata 解析为 dict 时（保留嵌套结构的解析器）也能取到。"""

        meta = {"name": "s", "metadata": {"author": "Microsoft", "version": "2.0.0"}}
        assert extract_declared_version(meta) == "2.0.0"

    def test_top_level_wins_over_nested(self):
        meta = {"version": "1.0.0", "metadata": {"version": "9.9.9"}}
        assert extract_declared_version(meta) == "1.0.0"

    def test_missing_version_returns_none(self):
        assert extract_declared_version({"name": "s"}) is None
        assert extract_declared_version({"metadata": {"author": "x"}}) is None


class TestExtractDeclaredTags:
    """frontmatter tags 归一化：逗号分隔字符串 / YAML 列表 / 嵌套 metadata 块。"""

    def _parse(self, text: str) -> dict:
        meta, _ = parse_skill_frontmatter_text(text)
        return meta

    def test_comma_separated_string_split(self):
        """datadog-labs 风格：metadata 块下逗号分隔字符串，拆成多个 tag。"""

        meta = self._parse(
            "---\n"
            "name: setup-sourcemaps\n"
            "description: Upload sourcemaps.\n"
            "metadata:\n"
            '  tags: "browser-sdk, sourcemaps, rum"\n'
            "---\n"
            "# body\n"
        )
        assert extract_declared_tags(meta) == ["browser-sdk", "sourcemaps", "rum"]

    def test_inline_list_tags(self):
        meta = self._parse(
            "---\nname: s\ndescription: d\ntags: [python, \"fast api\"]\n---\nbody\n"
        )
        assert extract_declared_tags(meta) == ["python", "fast api"]

    def test_space_separated_string_split(self):
        """NVIDIA blueprint 风格：metadata 块下空格分隔字符串，拆成多个 tag。"""

        meta = self._parse(
            "---\n"
            "name: vss-setup-behavior-analytics\n"
            "description: Deploy standalone service.\n"
            "metadata:\n"
            '  tags: "nvidia blueprint operational deployment behavior-analytics"\n'
            "---\n"
            "# body\n"
        )
        assert extract_declared_tags(meta) == [
            "nvidia", "blueprint", "operational", "deployment", "behavior-analytics",
        ]

    def test_block_list_preserves_multi_word_tags(self):
        """YAML 列表项是结构化声明，项内空格属于 tag 名，不拆分。"""

        meta = self._parse(
            "---\n"
            "name: s\n"
            "description: d\n"
            "tags:\n"
            "  - Extended Context\n"
            "  - NeMo Curator\n"
            "---\n"
            "# body\n"
        )
        assert extract_declared_tags(meta) == ["Extended Context", "NeMo Curator"]

    def test_block_list_tags(self):
        meta = self._parse(
            "---\nname: s\ndescription: d\ntags:\n  - python\n  - asyncio\n---\nbody\n"
        )
        assert extract_declared_tags(meta) == ["python", "asyncio"]

    def test_single_string_and_dedupe(self):
        meta = self._parse(
            "---\nname: s\ndescription: d\ntags: \"python, python , , asyncio\"\n---\nbody\n"
        )
        assert extract_declared_tags(meta) == ["python", "asyncio"]

    def test_top_level_wins_over_nested_dict(self):
        meta = {"tags": "a, b", "metadata": {"tags": "x, y"}}
        assert extract_declared_tags(meta) == ["a", "b"]

    def test_nested_dict_tags_fallback(self):
        meta = {"name": "s", "metadata": {"tags": "x, y"}}
        assert extract_declared_tags(meta) == ["x", "y"]

    def test_missing_tags_returns_none(self):
        assert extract_declared_tags({"name": "s"}) is None
        assert extract_declared_tags({"tags": " , "}) is None


class TestGitOperationsTags:
    def _init_repository(self, path: Path) -> None:
        path.mkdir(parents=True)
        _git(path, "init")
        _git(path, "config", "user.email", "tests@example.com")
        _git(path, "config", "user.name", "WittyHub Tests")

    def test_tag_sort_key_orders_versions_naturally(self):

        key = GitOperations._tag_sort_key
        assert key("v10.0.0") > key("v9.0.0")
        assert key("v2.1.10") > key("v2.1.9")
        assert key("2.0") > key("1.9")

    def test_sync_catalog_repository_clones_when_absent(self, tmp_path):
        origin = tmp_path / "origin.git"
        self._init_repository(origin)
        (origin / "skill.yaml").write_text("name: t\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "init")

        target = tmp_path / "catalog"
        GitOperations(github_token=None).sync_catalog_repository(target, str(origin))

        assert (target / ".git").exists()
        assert (target / "skill.yaml").exists()

    def test_sync_catalog_repository_pulls_when_present(self, tmp_path):
        origin = tmp_path / "origin.git"
        self._init_repository(origin)
        (origin / "skill.yaml").write_text("name: t\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "init")

        target = tmp_path / "catalog"
        GitOperations(github_token=None).sync_catalog_repository(target, str(origin))

        # 仓库端新增提交后再次同步走 pull --ff-only
        (origin / "skill.yaml").write_text("name: t2\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "update")
        GitOperations(github_token=None).sync_catalog_repository(target, str(origin))

        assert (target / "skill.yaml").read_text(encoding="utf-8") == "name: t2\n"

    def test_sync_catalog_repository_replaces_stale_non_git_dir(self, tmp_path):
        """目标目录已存在但非 git 仓库（clone 中断残留）→ 清掉后重新 clone。"""
        origin = tmp_path / "origin.git"
        self._init_repository(origin)
        (origin / "skill.yaml").write_text("name: t\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "init")

        target = tmp_path / "catalog"
        target.mkdir()
        (target / "garbage.txt").write_text("stale", encoding="utf-8")

        GitOperations(github_token=None).sync_catalog_repository(target, str(origin))

        assert (target / ".git").exists()
        assert not (target / "garbage.txt").exists()

    def test_sync_catalog_repository_empty_dir_clones(self, tmp_path):
        """目标目录存在但为空（clone 中断残留）→ 直接 clone 不报 File exists。"""
        origin = tmp_path / "origin.git"
        self._init_repository(origin)
        (origin / "skill.yaml").write_text("name: t\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "init")

        target = tmp_path / "catalog"
        target.mkdir()

        GitOperations(github_token=None).sync_catalog_repository(target, str(origin))

        assert (target / ".git").exists()

    def test_sync_catalog_repository_concurrent_calls_no_file_exists(self, tmp_path):
        """并发调用不报 File exists（进程内互斥锁保护 check-then-act）。

        复现：API 启动时 discover 调度与分类补齐同时调用
        sync_openEuler_skills_repo → 两个线程同时 clone 同一目录，
        第二个撞上 "File exists"。
        """
        origin = tmp_path / "origin.git"
        self._init_repository(origin)
        (origin / "skill.yaml").write_text("name: t\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "init")

        target = tmp_path / "catalog"
        errors: list[Exception] = []

        def sync():
            try:
                GitOperations(github_token=None).sync_catalog_repository(
                    target, str(origin),
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=sync) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert (target / ".git").exists()

    def test_sync_catalog_repository_replaces_dangling_symlink(self, tmp_path):
        """目标是断链符号链接时 unlink 后重新 clone，不报 File exists。

        复现：宿主机 dev 目录被 symlink 到容器路径但未挂载进容器——
        symlink 的 exists() 为 False（漏过 rmtree），git clone 却按链接
        本身存在判定，报 "could not create work tree dir: File exists"。
        """
        origin = tmp_path / "origin.git"
        self._init_repository(origin)
        (origin / "skill.yaml").write_text("name: t\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "init")

        target = tmp_path / "catalog"
        os.symlink(tmp_path / "not-mounted-anywhere", target)
        assert target.is_symlink() and not target.exists()  # 断链

        GitOperations(github_token=None).sync_catalog_repository(target, str(origin))

        assert not target.is_symlink()
        assert (target / ".git").exists()
        assert (target / "skill.yaml").exists()

    def test_sync_catalog_repository_keeps_valid_symlink_target(self, tmp_path):
        """symlink 指向真实 git 仓库时走 pull 分支，只更新、不删链接。"""
        origin = tmp_path / "origin.git"
        self._init_repository(origin)
        (origin / "skill.yaml").write_text("name: t\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "init")

        real_target = tmp_path / "real-catalog"
        GitOperations(github_token=None).sync_catalog_repository(real_target, str(origin))

        link = tmp_path / "catalog"
        os.symlink(real_target, link)

        (origin / "skill.yaml").write_text("name: t2\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "update")
        GitOperations(github_token=None).sync_catalog_repository(link, str(origin))

        assert link.is_symlink()
        assert (real_target / "skill.yaml").read_text(encoding="utf-8") == "name: t2\n"

    def test_cleanup_orphaned_tmp_packs_only_removes_temporaries(self, tmp_path):

        pack_dir = tmp_path / ".git" / "objects" / "pack"
        pack_dir.mkdir(parents=True)
        orphaned = pack_dir / "tmp_pack_yqbJ9L"
        orphaned.write_bytes(b"partial transfer")
        real_pack = pack_dir / "pack-abc123.pack"
        real_pack.write_bytes(b"real pack")
        keep_idx = pack_dir / "pack-abc123.idx"
        keep_idx.write_bytes(b"real idx")

        GitOperations._cleanup_orphaned_tmp_packs(tmp_path)

        assert not orphaned.exists()
        assert real_pack.exists()
        assert keep_idx.exists()

    def test_list_remote_tags_parses_and_sorts(self, tmp_path):
        origin = tmp_path / "origin.git"
        self._init_repository(origin)
        (origin / "SKILL.md").write_text("---\nname: t\n---\n# t\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "init")
        for tag in ("v1.0.0", "v1.0.1", "v1.1.0", "v2.0.0", "v2.10.0", "v9.0.0"):
            _git(origin, "tag", tag)

        clone_dir = tmp_path / "clone"
        _git(tmp_path, "clone", "--depth", "1", "--no-checkout", "--filter=blob:none",
             str(origin), str(clone_dir))

        git_ops = GitOperations(github_token=None, max_tags_per_repo=3)
        tags = git_ops._list_remote_tags(clone_dir, str(origin), None)

        assert tags[:3] == ["v9.0.0", "v2.10.0", "v2.0.0"]

    def test_get_repository_latest_tags_fetches_only_selected_tags(self, tmp_path):
        origin = tmp_path / "origin.git"
        self._init_repository(origin)
        # Allow blob:none filter over the file:// transport.
        _git(origin, "config", "uploadpack.allowFilter", "true")
        (origin / "SKILL.md").write_text("---\nname: t\n---\n# t\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "init")
        for tag in ("v1.0.0", "v1.0.1", "v2.0.0"):
            (origin / "CHANGELOG.md").write_text(f"# {tag}\n", encoding="utf-8")
            _git(origin, "add", ".")
            _git(origin, "commit", "-m", f"release {tag}")
            _git(origin, "tag", tag)
        # HEAD sits after every tag so the shallow clone carries no tags.
        (origin / "README.md").write_text("# after tags\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "post-release commit")

        clone_dir = tmp_path / "clone"
        # file:// keeps --depth/--filter effective (plain paths ignore them).
        _git(tmp_path, "clone", "--depth", "1", "--no-checkout", "--filter=blob:none",
             f"file://{origin}", str(clone_dir))
        assert _git(clone_dir, "tag").splitlines() == []

        git_ops = GitOperations(github_token=None, max_tags_per_repo=2)
        tags = git_ops._get_repository_latest_tags(clone_dir, f"file://{origin}", None)

        assert tags == ["v2.0.0", "v1.0.1"]
        # Only the selected tags land in the clone; v1.0.0 must stay unfetched.
        local_tags = _git(clone_dir, "tag").splitlines()
        assert local_tags == ["v1.0.1", "v2.0.0"]

    def test_get_repository_latest_tags_orders_irregular_names_by_date(self, tmp_path):
        origin = tmp_path / "origin.git"
        self._init_repository(origin)
        _git(origin, "config", "uploadpack.allowFilter", "true")

        tag_dates = (
            ("v99.0.0", "2024-01-01T00:00:00+00:00"),
            ("release-final", "2025-01-01T00:00:00+00:00"),
            ("stable-summer", "2026-01-01T00:00:00+00:00"),
        )
        for tag, date in tag_dates:
            (origin / "SKILL.md").write_text(
                f"---\nname: {tag}\n---\n# {tag}\n", encoding="utf-8",
            )
            _git(origin, "add", ".")
            commit_env = os.environ.copy()
            commit_env.update({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
            _git(origin, "commit", "-m", f"release {tag}", env=commit_env)
            _git(origin, "tag", tag)

        clone_dir = tmp_path / "clone"
        _git(
            tmp_path, "clone", "--depth", "1", "--no-checkout", "--filter=blob:none",
            f"file://{origin}", str(clone_dir),
        )

        git_ops = GitOperations(github_token=None, max_tags_per_repo=2)
        tags = git_ops._get_repository_latest_tags(clone_dir, f"file://{origin}", None)

        assert tags == ["stable-summer", "release-final"]
        assert _git(clone_dir, "tag").splitlines() == ["release-final", "stable-summer"]

    def test_get_repository_latest_tags_cleans_orphaned_packs_on_failure(self, tmp_path):
        origin = tmp_path / "origin.git"
        self._init_repository(origin)
        (origin / "SKILL.md").write_text("---\nname: t\n---\n# t\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-m", "init")
        _git(origin, "tag", "v1.0.0")

        clone_dir = tmp_path / "clone"
        _git(tmp_path, "clone", "--depth", "1", "--no-checkout", "--filter=blob:none",
             str(origin), str(clone_dir))

        pack_dir = clone_dir / ".git" / "objects" / "pack"
        pack_dir.mkdir(parents=True, exist_ok=True)
        orphaned = pack_dir / "tmp_pack_orphan"
        orphaned.write_bytes(b"partial transfer")

        git_ops = GitOperations(github_token=None, max_tags_per_repo=1)
        with patch.object(
            GitOperations, "_run_git_command_with_auth_retry",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1),
        ):
            tags = git_ops._get_repository_latest_tags(clone_dir, str(origin), None)

        assert tags == []
        assert not orphaned.exists()


class TestGitOperationsMaterialize:
    def _init_repository(self, path: Path) -> None:
        path.mkdir(parents=True)
        _git(path, "init")
        _git(path, "config", "user.email", "tests@example.com")
        _git(path, "config", "user.name", "WittyHub Tests")

    def test_materialize_skill_objects_fetches_missing_blobs(self, tmp_path):

        origin = tmp_path / "origin.git"
        self._init_repository(origin)
        _git(origin, "config", "uploadpack.allowFilter", "true")
        skill_dir = origin / ".agents" / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n# demo\n", encoding="utf-8")
        (skill_dir / "run.sh").write_text("#!/bin/sh\necho demo\n", encoding="utf-8")

        commit = None
        for round_index in range(2):
            (skill_dir / "run.sh").write_text(
                f"#!/bin/sh\necho demo v{round_index}\n", encoding="utf-8",
            )
            _git(origin, "add", ".")
            _git(origin, "commit", "-m", f"change {round_index}")
            commit = _git(origin, "rev-parse", "HEAD")

        clone_dir = tmp_path / "clone"
        _git(tmp_path, "clone", "--depth", "1", "--no-checkout", "--filter=blob:none",
             f"file://{origin}", str(clone_dir))
        # Older commits stay unfetched in the shallow partial clone.
        older_commit = _git(origin, "rev-parse", "HEAD~1")
        _git(clone_dir, "fetch", "--depth", "1", "--filter=blob:none",
             f"origin", f"{older_commit}")

        def blobs_missing(repo: Path, ref: str, path: str) -> bool:
            objects = _git(
                repo, "ls-tree", "-r",
                "--format=%(objectname) %(objecttype)", ref, "--", path,
            ).splitlines()
            shas = [line.split()[0] for line in objects if line.split()[1] == "blob"]
            # Query existence without triggering a lazy fetch.
            result = subprocess.run(
                ["git", "-C", str(repo), "cat-file", "--batch-check"],
                input="\n".join(shas) + "\n",
                capture_output=True, text=True,
                env={
                    **os.environ,
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_NO_LAZY_FETCH": "1",
                },
            )
            return " missing" in result.stdout

        assert blobs_missing(clone_dir, older_commit, ".agents/skills/demo")

        git_ops = GitOperations(github_token=None)
        assert git_ops.materialize_skill_objects(
            clone_dir, older_commit, ".agents/skills/demo",
        ) is True
        assert not blobs_missing(clone_dir, older_commit, ".agents/skills/demo")

    def test_materialize_skill_objects_without_git_dir_returns_false(self, tmp_path):
        git_ops = GitOperations(github_token=None)
        assert git_ops.materialize_skill_objects(tmp_path, "HEAD", "skills/demo") is False

    def test_materialize_retries_after_transient_network_error(self, tmp_path, caplog):
        """网络抖动中断懒拉取后重试续拉（已缓存 blob 自动跳过），日志带 git stderr。"""

        (tmp_path / ".git").mkdir()
        git_ops = GitOperations(github_token=None)

        listing = "aaa111 blob 3\nbbb222 blob 4\nccc333 tree 1\n"
        failure = subprocess.CalledProcessError(
            128, ["git", "cat-file", "--batch-check"],
            output="", stderr="OpenSSL SSL_connect: SSL_ERROR_SYSCALL",
        )
        success = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="aaa111 blob 3\nbbb222 blob 4\n", stderr="",
        )

        caplog.set_level(logging.WARNING, logger="src.utils.git_operations")
        with (
            # 首次调用为 cat-file -e 存在性检查，其后为 ls-tree 列举
            patch.object(GitOperations, "_run_git_command", side_effect=["", listing]),
            patch(
                "src.utils.git_operations.subprocess.run",
                side_effect=[failure, success],
            ) as run_mock,
        ):
            assert git_ops.materialize_skill_objects(tmp_path, "HEAD", "skills/demo") is True

        assert run_mock.call_count == 2
        assert "SSL_ERROR_SYSCALL" in caplog.text
        assert f"(attempt 1/{MATERIALIZE_ATTEMPTS})" in caplog.text

    def test_materialize_fails_after_exhausting_retries(self, tmp_path, caplog):
        """全部尝试失败返回 False，每次告警都带 git stderr 便于排查。"""

        (tmp_path / ".git").mkdir()
        git_ops = GitOperations(github_token=None)

        listing = "aaa111 blob 3\n"
        failure = subprocess.CalledProcessError(
            128, ["git", "cat-file", "--batch-check"],
            output="", stderr="could not fetch from promisor remote",
        )

        caplog.set_level(logging.WARNING, logger="src.utils.git_operations")
        with (
            # 首次调用为 cat-file -e 存在性检查，其后为 ls-tree 列举
            patch.object(GitOperations, "_run_git_command", side_effect=["", listing]),
            patch(
                "src.utils.git_operations.subprocess.run",
                side_effect=failure,
            ) as run_mock,
        ):
            assert git_ops.materialize_skill_objects(tmp_path, "HEAD", "skills/demo") is False

        assert run_mock.call_count == MATERIALIZE_ATTEMPTS
        assert "could not fetch from promisor remote" in caplog.text
        assert f"(attempt {MATERIALIZE_ATTEMPTS}/{MATERIALIZE_ATTEMPTS})" in caplog.text

    def test_materialize_rejects_nonexistent_skill_path(self, tmp_path, caplog):
        """skill_path 在 ref 下不存在时返回 False 而非静默通过。

        复现：路径少了目录前缀（如 asplore-orchestration 而非
        skills/aspire-orchestration）时，ls-tree 静默返回空列表，
        若不校验会发往 Jenkins 在 checkout 阶段才报 exit code 3。
        """
        (tmp_path / ".git").mkdir()
        git_ops = GitOperations(github_token=None)

        failure = subprocess.CalledProcessError(
            128, ["git", "cat-file", "-e"],
            output="", stderr="Not a valid object name",
        )
        caplog.set_level(logging.WARNING, logger="src.utils.git_operations")
        with patch.object(GitOperations, "_run_git_command", side_effect=failure):
            assert git_ops.materialize_skill_objects(
                tmp_path, "HEAD", "aspire-orchestration",
            ) is False

        assert "does not exist" in caplog.text


class TestDeepSeekCategoryClassifier:
    @staticmethod
    def _make_classifier():
        with patch(
            "src.ai.category_classifier.load_skill_categories",
            return_value=["Development and Build", "others"],
        ):
            return DeepSeekCategoryClassifier(repository_path=None)

    def test_classify_retries_transient_timeout_then_succeeds(self):

        classifier = self._make_classifier()
        with (
            patch.object(
                DeepSeekCategoryClassifier,
                "_call_deepseek",
                side_effect=[
                    httpx.ReadTimeout("The read operation timed out"),
                    "Development and Build",
                ],
            ) as call_mock,
            patch("src.ai.category_classifier.time.sleep") as sleep_mock,
        ):
            category = classifier.classify(
                skill_file=Path("SKILL.md"),
                metadata={},
                content="body",
                source_url="https://x",
            )

        assert category == "Development and Build"
        assert call_mock.call_count == 2
        sleep_mock.assert_called_once()

    def test_classify_degrades_to_frontmatter_after_transient_exhausted(self):
        """网络故障重试耗尽后沿用合法的 frontmatter category（归一化后），不 fatal 中断扫描。"""

        classifier = self._make_classifier()
        with (
            patch.object(
                DeepSeekCategoryClassifier,
                "_call_deepseek",
                side_effect=httpx.ReadTimeout("The read operation timed out"),
            ) as call_mock,
            patch.object(
                DeepSeekCategoryClassifier,
                "_normalize_category",
                # 第一次调用（入口校验）判非法以进入 LLM 路径，
                # 第二次调用（降级校验）归一为合法标准分类
                side_effect=[None, "Development and Build"],
            ),
            patch("src.ai.category_classifier.time.sleep"),
        ):
            category = classifier.classify(
                skill_file=Path("SKILL.md"),
                metadata={"category": "development and build"},
                content="body",
                source_url="https://x",
            )

        # 合法值经 _normalize_category 归一到类别表标准写法后沿用
        assert category == "Development and Build"
        assert call_mock.call_count == CATEGORY_RETRY_ATTEMPTS

    def test_classify_degrades_to_none_when_frontmatter_category_invalid(self):
        """frontmatter category 不在类别表内时降级返回 None，留待下轮重新分类。"""

        classifier = self._make_classifier()
        with (
            patch.object(
                DeepSeekCategoryClassifier,
                "_call_deepseek",
                side_effect=httpx.ReadTimeout("The read operation timed out"),
            ) as call_mock,
            patch("src.ai.category_classifier.time.sleep"),
        ):
            category = classifier.classify(
                skill_file=Path("SKILL.md"),
                metadata={"category": "Some Random Category"},
                content="body",
                source_url="https://x",
            )

        assert category is None
        assert call_mock.call_count == CATEGORY_RETRY_ATTEMPTS

    def test_classify_fatal_on_non_transient_error(self):
        """配置/凭据类错误保持 fatal 语义，且不做无意义重试。"""

        classifier = self._make_classifier()
        with (
            patch.object(
                DeepSeekCategoryClassifier,
                "_call_deepseek",
                side_effect=RuntimeError("invalid api key"),
            ) as call_mock,
            patch("src.ai.category_classifier.time.sleep") as sleep_mock,
        ):
            with pytest.raises(CategoryClassificationError):
                classifier.classify(
                    skill_file=Path("SKILL.md"),
                    metadata={},
                    content="body",
                    source_url="https://x",
                )

        assert call_mock.call_count == 1
        sleep_mock.assert_not_called()

    def test_is_transient_error_classification(self):

        request = httpx.Request("POST", "https://x")
        server_error = httpx.HTTPStatusError(
            "boom", request=request, response=httpx.Response(503, request=request)
        )
        auth_error = httpx.HTTPStatusError(
            "boom", request=request, response=httpx.Response(401, request=request)
        )

        assert DeepSeekCategoryClassifier._is_transient_error(httpx.ReadTimeout("t")) is True
        assert DeepSeekCategoryClassifier._is_transient_error(server_error) is True
        assert DeepSeekCategoryClassifier._is_transient_error(auth_error) is False
        assert DeepSeekCategoryClassifier._is_transient_error(RuntimeError("x")) is False


class TestSearchService:
    async def test_search_service_initialization(self):
        mock_session = MagicMock()
        service = SearchService(mock_session)
        assert service is not None
        assert service.session == mock_session

    def test_semantic_search_skips_text_search(self):

        service = SearchService(MagicMock())
        service._text_search = AsyncMock()
        service._vector_search = AsyncMock(
            return_value={"results": [], "total": 0, "mode": "semantic"}
        )

        result = asyncio.run(
            service.search_skills(
                query="rocm-kernels",
                embedding=[0.1, 0.2],
                mode="semantic",
            )
        )

        service._text_search.assert_not_awaited()
        service._vector_search.assert_awaited_once()
        assert result["mode"] == "semantic"

    def test_text_search_fetches_only_requested_page(self):
        service = SearchService(MagicMock())
        service._text_search = AsyncMock(
            return_value={"results": [{"skill_id": "skill-1"}], "total": 1}
        )
        service._vector_search = AsyncMock()

        result = asyncio.run(
            service.search_skills(
                query="code",
                limit=12,
                offset=24,
                mode="text",
            )
        )

        service._text_search.assert_awaited_once_with(
            query="code",
            limit=12,
            offset=24,
            category=None,
            platform=None,
            tags=None,
            scope="summary",
            security_level=None,
        )
        service._vector_search.assert_not_awaited()
        assert result["results"] == [{"skill_id": "skill-1"}]

    def test_hybrid_search_total_and_pagination_use_fused_candidates(self):
        service = SearchService(MagicMock())
        service._text_search = AsyncMock(
            return_value={
                "results": [
                    {"id": "1", "skill_id": "skill-1"},
                    {"id": "2", "skill_id": "skill-2"},
                ],
                "total": 2,
            }
        )
        service._vector_search = AsyncMock(
            return_value={
                "results": [
                    {"id": "3", "skill_id": "skill-3"},
                    {"id": "4", "skill_id": "skill-4"},
                    {"id": "1", "skill_id": "skill-1"},
                ],
                "total": 3,
                "mode": "semantic",
            }
        )

        result = asyncio.run(
            service.search_skills(
                query="日志分析",
                embedding=[0.1, 0.2],
                mode="hybrid",
                limit=2,
                offset=2,
            )
        )

        assert result["total"] == 4
        assert len(result["results"]) == 2
        assert result["skip"] == 2
        assert result["limit"] == 2
        assert service._text_search.await_args.kwargs["limit"] == HYBRID_CANDIDATE_LIMIT
        assert service._text_search.await_args.kwargs["offset"] == 0
        assert service._vector_search.await_args.kwargs["limit"] == HYBRID_CANDIDATE_LIMIT
        assert service._vector_search.await_args.kwargs["offset"] == 0


class TestAsyncNonBlocking:
    """扫描链路的同步阻塞调用（git/LLM）必须放线程池，不冻结事件循环。"""

    async def test_discover_scan_does_not_block_event_loop(self):

        manager = SkillManager(MagicMock(), AsyncMock())
        started = time.perf_counter()

        def slow_sync_git(*_args, **_kwargs):
            # 模拟网络挂起的同步 git 调用（如 github 不可达时的 fetch）
            time.sleep(0.4)

        probe_elapsed: list[float] = []

        async def probe():
            await asyncio.sleep(0.1)
            probe_elapsed.append(time.perf_counter() - started)

        with (
            patch.object(SkillManager, "_sync_git_repository", slow_sync_git),
            patch.object(SkillManager, "_list_skill_paths", return_value=[]),
        ):
            await asyncio.gather(
                manager.discover_configured_skill_repository(
                    SkillRepositoryRequest(url="https://github.com/acme/agent-skills")
                ),
                probe(),
            )

        # 若 git 调用阻塞事件循环，probe 只能等 0.4s 后才完成
        assert probe_elapsed and probe_elapsed[0] < 0.3


class TestDeleteCleanup:
    """删除仓库/技能时的软关联清理与统计自愈。"""

    async def test_delete_skill_repositories_cleans_skill_and_version_soft_links(self):
        skill_one, skill_two, version_one = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                SimpleNamespace(all=lambda: [(skill_one,), (skill_two,)]),
                SimpleNamespace(all=lambda: [(version_one,)]),
                SimpleNamespace(rowcount=1),
            ]
        )
        related_mock = AsyncMock(return_value=3)
        with patch(
            "src.models.repository.SkillRepository.delete_skill_related_records",
            related_mock,
        ):
            deleted = await SkillRepoRepository(session).delete_skill_repositories(
                [uuid.uuid4()]
            )

        assert deleted == 1
        # 软关联清理收到 skill 与 skill_versions 两类 UUID 的并集
        assert set(related_mock.await_args.args[0]) == {
            skill_one, skill_two, version_one,
        }

    async def test_skill_delete_cleans_version_soft_links(self):
        skill_uuid, version_one, version_two = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                SimpleNamespace(scalar_one_or_none=lambda: skill_uuid),
                SimpleNamespace(all=lambda: [(version_one,), (version_two,)]),
                SimpleNamespace(rowcount=2),
                SimpleNamespace(rowcount=1),
            ]
        )
        related_mock = AsyncMock()
        with patch(
            "src.models.repository.SkillRepository.delete_skill_related_records",
            related_mock,
        ):
            result = await SkillRepository(session).delete("atomgit:acme/widgets/one")

        assert result is True
        assert set(related_mock.await_args.args[0]) == {
            skill_uuid, version_one, version_two,
        }

    async def test_delete_skill_repository_reconciles_statistics(self):
        manager = SkillManager(MagicMock(), AsyncMock())
        stored = SimpleNamespace(id=uuid.uuid4(), local_path=None)
        reconcile_mock = AsyncMock()
        with (
            patch.object(
                SkillManager,
                "get_repository_by_id",
                AsyncMock(return_value=stored),
            ),
            patch.object(SkillManager, "_reconcile_skill_statistics", reconcile_mock),
        ):
            await manager.delete_skill_repository(str(stored.id))

        reconcile_mock.assert_awaited_once()


class TestSkillUrlContribution:
    """skills[].skill_url 单 Skill 贡献：URL 解析、请求合并与收窄扫描。"""

    def test_parse_skill_md_url_parses_repo_ref_and_path(self):
        repo_url, ref, skill_path = parse_skill_md_url(
            "https://github.com/example/skills.git/blob/main/skills/demo/SKILL.md"
        )
        assert repo_url == "https://github.com/example/skills"
        assert ref == "main"
        assert skill_path == "skills/demo/SKILL.md"

        # atomgit 旧域名规范化为 atomgit；查询串/锚点忽略
        repo_url, ref, skill_path = parse_skill_md_url(
            "https://atomgit.com/acme/x/blob/master/SKILL.md?plain=1"
        )
        assert repo_url == "https://atomgit.com/acme/x"
        assert ref == "master"
        assert skill_path == "SKILL.md"

    def test_parse_skill_md_url_rejects_invalid(self):
        for bad in (
            "",
            "https://github.com/acme/x/tree/main",
            "https://github.com/acme/x/blob/main/README.md",
            "https://github.com/acme/x/blob//SKILL.md",
        ):
            with pytest.raises(ValueError):
                parse_skill_md_url(bad)

    def test_load_catalog_skill_url_entries_collects_skills(self, tmp_path):
        skill_file = tmp_path / "community" / "sig-demo" / "skill.yaml"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text(
            "skills:\n"
            "  - skill_name: demo\n"
            "    skill_url: https://github.com/acme/tools/blob/main/skills/demo/SKILL.md\n",
            encoding="utf-8",
        )

        entries = load_catalog_skill_url_entries(tmp_path)

        assert entries == [
            {
                "platform": "community",
                "owner": "sig-demo",
                "skill_name": "demo",
                "skill_url": "https://github.com/acme/tools/blob/main/skills/demo/SKILL.md",
            }
        ]

    def test_build_requests_merges_skill_urls_per_repo(self, tmp_path):
        skill_file = tmp_path / "community" / "sig-demo" / "skill.yaml"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text(
            "skills:\n"
            "  - skill_name: one\n"
            "    skill_url: https://github.com/acme/tools/blob/main/skills/one/SKILL.md\n"
            "  - skill_name: two\n"
            "    skill_url: https://github.com/acme/tools/blob/main/skills/two/SKILL.md\n",
            encoding="utf-8",
        )

        requests, _ = build_configured_discover_requests(repository_path=tmp_path)

        # 同仓库多条贡献合并为一个请求，路径取并集
        assert len(requests) == 1
        request = requests[0]
        assert request.url == "https://github.com/acme/tools"
        assert request.branch == "main"
        assert request.platform == "community"
        assert request.sig_name == "sig-demo"
        assert request.skill_paths == ["skills/one/SKILL.md", "skills/two/SKILL.md"]

    def test_build_requests_skips_skill_urls_covered_by_skill_repos(self, tmp_path):
        skill_file = tmp_path / "community" / "sig-demo" / "skill.yaml"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text(
            "skill_repos:\n"
            "  - url: https://github.com/acme/tools.git\n"
            "skills:\n"
            "  - skill_name: one\n"
            "    skill_url: https://github.com/acme/tools/blob/main/skills/one/SKILL.md\n",
            encoding="utf-8",
        )

        requests, _ = build_configured_discover_requests(repository_path=tmp_path)

        # 整仓登记覆盖单 Skill 贡献（.git 后缀规范化后比对），保持整仓扫描
        assert len(requests) == 1
        assert requests[0].url == "https://github.com/acme/tools.git"
        assert requests[0].skill_paths is None

    def test_build_requests_rejects_mixed_refs_for_same_repo(self, tmp_path):
        skill_file = tmp_path / "community" / "sig-demo" / "skill.yaml"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text(
            "skills:\n"
            "  - skill_name: one\n"
            "    skill_url: https://github.com/acme/tools/blob/main/skills/one/SKILL.md\n"
            "  - skill_name: two\n"
            "    skill_url: https://github.com/acme/tools/blob/dev/skills/two/SKILL.md\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="must share one ref"):
            build_configured_discover_requests(repository_path=tmp_path)

    def test_build_requests_rejects_missing_skill_name(self, tmp_path):
        skill_file = tmp_path / "community" / "sig-demo" / "skill.yaml"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text(
            "skills:\n"
            "  - skill_url: https://github.com/acme/tools/blob/main/skills/one/SKILL.md\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="skill_name and skill_url"):
            build_configured_discover_requests(repository_path=tmp_path)

    async def test_configured_discover_skill_paths_restricts_scan_scope(self):
        """URL 方式贡献：请求 skill_paths 非空时只扫描登记的 SKILL.md。"""

        skill_repository = MagicMock()
        skill_repository.list_unscored_by_skill_repo = AsyncMock(return_value=([], []))
        manager = SkillManager(skill_repository, AsyncMock())
        discover_mock = AsyncMock()
        repository = SimpleNamespace(
            id=uuid.uuid4(),
            repo_name="acme_tools",
            source="github",
            platform=None,
            skill_num=1,
        )
        with (
            patch.object(SkillManager, "_sync_git_repository"),
            patch.object(
                SkillManager,
                "_list_skill_paths",
                return_value=["skills/one/SKILL.md", "skills/two/SKILL.md"],
            ),
            patch.object(SkillManager, "_set_discovery_status", AsyncMock()),
            patch.object(
                SkillManager,
                "_get_or_create_skill_repository",
                AsyncMock(return_value=(repository, True)),
            ),
            patch.object(SkillManager, "_discover_and_store_skills", discover_mock),
        ):
            await manager.discover_configured_skill_repository(
                SkillRepositoryRequest(
                    url="https://github.com/acme/tools",
                    skill_paths=["skills/one/SKILL.md"],
                )
            )

        kwargs = discover_mock.await_args.kwargs
        assert kwargs["skill_paths"] == ["skills/one/SKILL.md"]
        assert kwargs["skill_path_filter"] == {"skills/one/SKILL.md"}

    async def test_start_scan_skill_path_filter_restricts_head_and_tags(self, tmp_path):
        """收窄扫描：HEAD 与 tag 版本都只收录登记的 SKILL.md。"""

        repository = tmp_path / "repository"
        repository.mkdir()
        _git(repository, "init")
        _git(repository, "config", "user.email", "tests@example.com")
        _git(repository, "config", "user.name", "WittyHub Tests")

        for name in ("one", "two"):
            skill_dir = repository / "skills" / name
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Skill {name} fixture.\n---\n{VALID_SKILL_BODY}\n", encoding="utf-8",
            )
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "add two skills")
        head_commit = _git(repository, "rev-parse", "HEAD")
        _git(repository, "tag", "v1")

        skill_repository = MagicMock()
        skill_repository.load_scan_records = AsyncMock(return_value=({}, {}))
        scanner = SkillScanner(
            git_ops=GitOperations(),
            skill_repository=skill_repository,
            category_classifier=None,
        )
        repo = SimpleNamespace(
            id=uuid.uuid4(),
            source="atomgit",
            url="https://atomgit.com/acme/widgets",
            branch="master",
            platform=None,
        )

        skills, tagged_skills = await scanner.start_scan(
            repo=repo,
            repo_root=repository,
            repository_git_metadata={
                "commit_id": head_commit,
                "latest_tags": ["v1"],
                "latest_tag_commits": {"v1": head_commit},
            },
            skill_paths=["skills/one/SKILL.md"],
            skill_path_filter={"skills/one/SKILL.md"},
        )

        assert [skill.skill_id for skill in skills] == ["atomgit:acme/widgets/one"]
        assert {v.skill_id for v in tagged_skills} == {"atomgit:acme/widgets/one"}


class TestConfig:
    async def test_get_settings(self):
        with patch.dict(
            "os.environ",
            {
                "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/skillhub",
            },
        ):
            settings = get_settings()
            assert settings is not None
            assert "postgresql" in settings.postgres.url
            assert settings.postgres.sync_url.startswith("postgresql+psycopg2://")


class TestAPIRoutes:
    async def test_health_endpoint(self):
        assert router is not None
        routes = [route.path for route in router.routes]
        assert "/health" in routes


class TestSkillSchema:
    async def test_skill_id_format(self):
        skill_data = {
            "id": str(uuid.uuid4()),
            "skill_id": "author/skill-name:v1.0.0",
            "name": "skill-name",
            "description": "Test",
            "version": "v1.0.0",
            "commit_id": "abc123",
            "author": "author",
            "source": "clawhub",
            "source_url": "https://example.com",
            "category": "Testing",
            "tags": ["test"],
            "platform": "openclaw",
            "extra_metadata": {},
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        response = SkillResponse(**skill_data)
        assert ":" in response.skill_id
        assert response.skill_id.startswith("author/")

    async def test_source_validation(self):
        valid_sources = ["local", "github", "clawhub", "atomgit", "gitlab"]
        for source in valid_sources:
            skill_data = {
                "id": str(uuid.uuid4()),
                "skill_id": "test/skill:v1.0.0",
                "name": "skill",
                "description": "Test",
                "version": "v1.0.0",
                "commit_id": "abc123",
                "author": "test",
                "source": source,
                "source_url": "https://example.com",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            response = SkillResponse(**skill_data)
            assert response.source == source
