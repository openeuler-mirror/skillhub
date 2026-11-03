import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

from src.api.services.telemetry import TelemetryService, build_skill_id_from_telemetry
from src.models.orm import DownloadHistory


class TestSkillsTelemetry:
    def test_build_skill_id_from_telemetry_uses_source_type_source_and_skill(self):
        skill_id = build_skill_id_from_telemetry(
            "github",
            "vercel-labs/agent-skills",
            "deploy-to-vercel",
        )

        assert skill_id == "github:vercel-labs/agent-skills/deploy-to-vercel"

    def test_build_skill_id_from_telemetry_prefers_skill_files_path(self):
        skill_id = build_skill_id_from_telemetry(
            "github",
            "vercel-labs/agent-skills",
            "Deploy to Vercel",
            {"Deploy to Vercel": "skills/deploy-to-vercel/SKILL.md"},
        )

        assert skill_id == "github:vercel-labs/agent-skills/deploy-to-vercel"

    def test_build_skill_id_from_telemetry_root_skill_matches_crawler(self):
        # Root-level SKILL.md resolves to the repo slug — same as the crawler
        # (build_public_skill_id), so the lookup hits the same DB record.
        skill_id = build_skill_id_from_telemetry(
            "github",
            "acme/agent-skills",
            "agent-skills",
            {"agent-skills": "SKILL.md"},
        )

        assert skill_id == "github:acme/agent-skills/agent-skills"

    def test_build_skill_id_from_telemetry_atomgit_matches_crawler(self):
        skill_id = build_skill_id_from_telemetry(
            "atomgit",
            "openeuler/yuanrong",
            "atomgit-api",
            {"atomgit-api": ".skills/atomgit-api/SKILL.md"},
        )

        assert skill_id == "atomgit:openeuler/yuanrong/atomgit-api"

    def test_process_install_telemetry_increments_each_matched_skill(self):
        service = TelemetryService(AsyncMock())
        service.skill_repo = AsyncMock()
        service.session.add = MagicMock()
        service.session.commit = AsyncMock()
        skill_uuid_1, skill_uuid_2 = uuid.uuid4(), uuid.uuid4()
        service.skill_repo.increment_download.side_effect = [skill_uuid_1, skill_uuid_2]
        params = {
            "event": "install",
            "source": "vercel-labs/agent-skills",
            "sourceType": "github",
            "skills": "deploy-to-vercel,create-sdk-plugin",
            "skillFiles": (
                '{"deploy-to-vercel":"skills/deploy-to-vercel/SKILL.md",'
                '"create-sdk-plugin":"skills/create-sdk-plugin/SKILL.md"}'
            ),
        }

        matched_skill_ids = asyncio.run(
            service.process(params, ip_address="127.0.0.1", user_agent="skillhub-cli/1.0")
        )

        assert matched_skill_ids == [
            "github:vercel-labs/agent-skills/deploy-to-vercel",
            "github:vercel-labs/agent-skills/create-sdk-plugin",
        ]
        assert service.skill_repo.increment_download.await_count == 2
        service.skill_repo.increment_download.assert_any_await(
            "github:vercel-labs/agent-skills/deploy-to-vercel"
        )
        service.skill_repo.increment_download.assert_any_await(
            "github:vercel-labs/agent-skills/create-sdk-plugin"
        )
        # 每个命中 Skill 落一条 DownloadHistory 记录，携带 IP/User-Agent
        added_records = [c.args[0] for c in service.session.add.call_args_list]
        assert len(added_records) == 2
        assert all(isinstance(r, DownloadHistory) for r in added_records)
        assert {r.resource_id for r in added_records} == {skill_uuid_1, skill_uuid_2}
        assert all(r.resource_type == "skill" for r in added_records)
        assert all(r.ip_address == "127.0.0.1" for r in added_records)
        assert all(r.user_agent == "skillhub-cli/1.0" for r in added_records)
        service.session.commit.assert_awaited_once()

    def test_process_install_telemetry_skips_unmatched_skill_ids(self):
        service = TelemetryService(AsyncMock())
        service.skill_repo = AsyncMock()
        service.session.add = MagicMock()
        service.session.commit = AsyncMock()
        skill_uuid = uuid.uuid4()
        service.skill_repo.increment_download.side_effect = [skill_uuid, None]
        params = {
            "event": "install",
            "source": "vercel-labs/agent-skills",
            "sourceType": "github",
            "skills": "deploy-to-vercel,missing-skill",
            "skillFiles": (
                '{"deploy-to-vercel":"skills/deploy-to-vercel/SKILL.md",'
                '"missing-skill":"skills/missing-skill/SKILL.md"}'
            ),
        }

        matched_skill_ids = asyncio.run(service.process(params))

        assert matched_skill_ids == [
            "github:vercel-labs/agent-skills/deploy-to-vercel",
        ]
        # 未命中的 Skill 不落 DownloadHistory 记录
        added_records = [c.args[0] for c in service.session.add.call_args_list]
        assert len(added_records) == 1
        assert added_records[0].resource_id == skill_uuid
        service.session.commit.assert_awaited_once()
