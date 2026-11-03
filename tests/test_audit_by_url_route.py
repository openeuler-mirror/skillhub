from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.api.routes.skills import audit_by_url, audit_by_url_report, audit_by_url_result
from src.api.schemas.skill import AuditByUrlRequest


def _service(result: dict | None = None, report_md: str | None = None) -> MagicMock:
    service = MagicMock()
    detector = MagicMock()
    detector.enable_audit = True
    service.detector = detector
    service.audit_external = AsyncMock(return_value=result)
    service.get_external_result = AsyncMock(return_value=result)
    service.get_external_report_md = AsyncMock(return_value=report_md)
    return service


@pytest.mark.asyncio
async def test_audit_by_url_repo_mode_derives_target():
    service = _service(
        result={
            "git_url": "https://atomgit.com/openeuler/foo",
            "ref": "main",
            "skill_path": "",
            "risk_level": "low",
            "risk_score": 10,
            "risk_signals": [],
            "details": {"scanners": ["skillspector"]},
        }
    )
    db = AsyncMock()
    with patch("src.api.routes.skills.SecurityService", return_value=service):
        response = await audit_by_url(
            payload=AuditByUrlRequest(repo_url="https://atomgit.com/openeuler/foo"),
            db=db,
        )
    assert response.risk_level == "low"
    assert response.risk_score == 10
    assert response.git_url == "https://atomgit.com/openeuler/foo"
    assert response.skill_path == ""
    service.audit_external.assert_awaited_once_with(
        git_url="https://atomgit.com/openeuler/foo",
        ref="main",
        skill_path="",
        scanners=None,
        async_mode=False,
    )


@pytest.mark.asyncio
async def test_audit_by_url_skill_mode_derives_blob_target():
    service = _service(
        result={
            "git_url": "https://atomgit.com/openeuler/foo",
            "ref": "main",
            "skill_path": "skills/bar",
            "risk_level": "high",
            "risk_score": 65,
            "risk_signals": [],
            "details": {"scanners": ["skillspector"]},
        }
    )
    db = AsyncMock()
    with patch("src.api.routes.skills.SecurityService", return_value=service):
        response = await audit_by_url(
            payload=AuditByUrlRequest(
                skill_url="https://atomgit.com/openeuler/foo/blob/main/skills/bar/SKILL.md"
            ),
            db=db,
        )
    assert response.git_url == "https://atomgit.com/openeuler/foo"
    assert response.ref == "main"
    assert response.skill_path == "skills/bar"
    assert response.risk_level == "high"


@pytest.mark.asyncio
async def test_audit_by_url_requires_a_url():
    db = AsyncMock()
    service = _service()
    with patch("src.api.routes.skills.SecurityService", return_value=service):
        with pytest.raises(HTTPException) as exc_info:
            await audit_by_url(payload=AuditByUrlRequest(), db=db)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_audit_by_url_rejects_non_whitelisted_host():
    db = AsyncMock()
    service = _service()
    with patch("src.api.routes.skills.SecurityService", return_value=service):
        with pytest.raises(HTTPException) as exc_info:
            await audit_by_url(
                payload=AuditByUrlRequest(repo_url="https://evil.example.com/foo/bar"),
                db=db,
            )
    assert exc_info.value.status_code == 400
    service.audit_external.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_by_url_rejects_when_disabled():
    db = AsyncMock()
    service = _service()
    service.detector.enable_audit = False
    with patch("src.api.routes.skills.SecurityService", return_value=service):
        with pytest.raises(HTTPException) as exc_info:
            await audit_by_url(
                payload=AuditByUrlRequest(repo_url="https://atomgit.com/openeuler/foo"),
                db=db,
            )
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_audit_by_url_result_done():
    service = _service(
        result={
            "status": "done",
            "build_number": 42,
            "jenkins_status": "SUCCESS",
            "risk_level": "high",
            "risk_score": 65,
            "risk_signals": [],
            "details": {"scanners": ["skillspector"]},
        }
    )
    db = AsyncMock()
    with patch("src.api.routes.skills.SecurityService", return_value=service):
        response = await audit_by_url_result(build_number=42, db=db)
    assert response.status == "done"
    assert response.build_number == 42
    assert response.risk_level == "high"
    assert response.risk_score == 65
    service.get_external_result.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_audit_by_url_result_pending():
    service = _service(
        result={
            "status": "pending",
            "build_number": 42,
            "jenkins_status": "BUILDING",
        }
    )
    db = AsyncMock()
    with patch("src.api.routes.skills.SecurityService", return_value=service):
        response = await audit_by_url_result(build_number=42, db=db)
    assert response.status == "pending"
    assert response.risk_level is None


@pytest.mark.asyncio
async def test_audit_by_url_result_error():
    service = _service(
        result={
            "status": "error",
            "build_number": 42,
            "error": "report fetch failed",
        }
    )
    db = AsyncMock()
    with patch("src.api.routes.skills.SecurityService", return_value=service):
        response = await audit_by_url_result(build_number=42, db=db)
    assert response.status == "error"
    assert response.error == "report fetch failed"


@pytest.mark.asyncio
async def test_audit_by_url_result_rejects_when_disabled():
    db = AsyncMock()
    service = _service()
    service.detector.enable_audit = False
    with patch("src.api.routes.skills.SecurityService", return_value=service):
        with pytest.raises(HTTPException) as exc_info:
            await audit_by_url_result(build_number=42, db=db)
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_audit_by_url_report_downloads_markdown():
    """report 端点以附件形式返回 report.md 内容，供 PR 评论详情链接下载."""
    db = AsyncMock()
    report_md = "# Security Report\n\n- high: prompt injection"
    service = _service(report_md=report_md)
    with patch("src.api.routes.skills.SecurityService", return_value=service):
        response = await audit_by_url_report(build_number=42, db=db)
    assert response.status_code == 200
    assert response.body == report_md.encode("utf-8")
    assert response.media_type == "text/markdown"
    assert "attachment" in response.headers["content-disposition"]
    assert "report-42.md" in response.headers["content-disposition"]
    service.get_external_report_md.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_audit_by_url_report_custom_filename():
    """report 端点支持门禁传入的自定义下载文件名（skill 名 + 安全审计报告.md）；
    中文等非 ASCII 名按 RFC 5987 用 filename*=UTF-8'' 携带，ASCII 名兜底，
    并剔除可能破坏 Content-Disposition 的字符."""
    db = AsyncMock()
    report_md = "# Security Report"
    service = _service(report_md=report_md)
    with patch("src.api.routes.skills.SecurityService", return_value=service):
        response = await audit_by_url_report(
            build_number=42, filename='witty-agents安全审计报告"换行.md', db=db
        )
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    # 非 ASCII 名走 filename*=UTF-8''，ASCII 兜底文件名 report-42.md
    assert "filename=\"report-42.md\"" in disposition
    assert "filename*=UTF-8''" in disposition
    assert "witty-agents" in disposition


@pytest.mark.asyncio
async def test_audit_by_url_report_ascii_filename():
    """纯 ASCII 自定义文件名直接放入 filename=，无需 filename*."""
    db = AsyncMock()
    report_md = "# Security Report"
    service = _service(report_md=report_md)
    with patch("src.api.routes.skills.SecurityService", return_value=service):
        response = await audit_by_url_report(
            build_number=42, filename="agent-ops-report.md", db=db
        )
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert 'filename="agent-ops-report.md"' in disposition
    assert "filename*" not in disposition


@pytest.mark.asyncio
async def test_audit_by_url_report_not_found():
    """report.md 取不到时返回 404."""
    db = AsyncMock()
    service = _service(report_md=None)
    with patch("src.api.routes.skills.SecurityService", return_value=service):
        with pytest.raises(HTTPException) as exc_info:
            await audit_by_url_report(build_number=42, db=db)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_audit_by_url_report_rejects_when_disabled():
    db = AsyncMock()
    service = _service()
    service.detector.enable_audit = False
    with patch("src.api.routes.skills.SecurityService", return_value=service):
        with pytest.raises(HTTPException) as exc_info:
            await audit_by_url_report(build_number=42, db=db)
    assert exc_info.value.status_code == 503
