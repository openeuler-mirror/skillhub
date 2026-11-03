import asyncio
import logging
import re
import subprocess
from typing import Annotated
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.skills.skill_id import extract_owner_repo

from src.api.schemas.skill import (
    AuditByUrlRequest,
    AuditByUrlResponse,
    AuditByUrlResultResponse,
    ContributorListResponse,
    ContributorResponse,
    AuditReportResponse,
    ErrorResponse,
    ContributorSkillsResponse,
    SecurityAuditResponse,
    SkillCreate,
    SkillListResponse,
    SkillResponse,
    SkillVersionsResponse,
)
from src.api.services.audit_report import build_audit_report
from src.api.services.security import SecurityService
from src.api.services.telemetry import TelemetryService
from src.core.auth import require_admin_token
from src.core.categories import category_label
from src.core.database import get_db
from src.core.rate_limit import limiter
from src.models.repository import (
    ContributorRepository,
    DownloadHistoryRepository,
    SkillRepoRepository,
    SkillRepository,
)
from src.security.detector import validate_git_url
from src.storage.downloader import (
    DownloadManager,
    SkillArchiveConflictError,
    SkillArchiveError,
    SkillArchiveNotFoundError,
)

router = APIRouter()
_logger = logging.getLogger(__name__)
SkillIdPath = Annotated[str, Path(min_length=1, max_length=255)]


def _derive_scan_skill_path(source: str, source_url: str, skill_id: str) -> str:
    """Derive the skill directory (relative to its git repo) for the Jenkins scan.

    Mirrors ``SkillManager._relative_skill_path`` so manual audits scan the same
    skill directory as crawler-triggered audits.  Empty string means repo root.
    """
    # 1) source_url 带 /blob/<ref>/.../SKILL.md 时直接取其父目录
    blob_match = re.search(r"/blob/[^/]+/(.+)", source_url or "")
    if blob_match:
        relative = blob_match.group(1)
        if relative.endswith("SKILL.md"):
            return relative.rsplit("/", 1)[0] if "/" in relative else ""

    # 2) 从 skill_id 推导：{source}/{owner}/{repo}/<skill dir>
    try:
        owner_repo = extract_owner_repo(source_url)
    except ValueError:
        return ""
    prefix = f"{source}/{owner_repo}"
    if skill_id == prefix:
        return ""  # 整仓库即 skill，扫仓库根
    if skill_id.startswith(prefix + "/"):
        skill_path = skill_id.removeprefix(prefix + "/").strip("/")
        repository_name = owner_repo.rsplit("/", 1)[-1]
        if not skill_path or skill_path == repository_name:
            return ""
        return skill_path
    return ""


def _resolve_default_branch(git_url: str) -> str:
    """Resolve the repository's default branch via ``git ls-remote --symref``.

    Falls back to ``"main"`` when the lookup fails (network error, URL not a
    git repository, ...) so the scan still gets a sensible target.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--symref", git_url, "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("ref:"):
                # 形如 "ref: refs/heads/master  HEAD"，按空白切分取第二列分支引用
                ref_spec = line.split()[1]
                if ref_spec.startswith("refs/heads/"):
                    return ref_spec.rsplit("/", 1)[-1]
    except (OSError, subprocess.SubprocessError):
        pass
    return "main"


def _derive_audit_target(payload: "AuditByUrlRequest") -> tuple[str, str, str]:
    """Derive ``(git_url, ref, skill_path)`` from an audit-by-url payload.

    纯字符串解析，不做任何网络请求（保证调用方可在解析后先做
    ``validate_git_url`` 校验，再决定是否发起网络访问）。

    * ``repo_url`` mode -> 整仓库扫描；``ref`` 仅在显式指定 ``payload.branch``
      时返回，未指定返回空串，由调用方在 URL 校验通过后解析默认分支。
    * ``skill_url`` mode (``<host>/<owner>/<repo>/blob/<ref>/<path>/SKILL.md``)
      -> 扫描单个 skill 目录，``ref`` 取自 URL 路径。
    """
    if payload.skill_url:
        parsed = urlparse(payload.skill_url.strip())
        segments = [segment for segment in parsed.path.strip("/").split("/") if segment]
        if len(segments) < 5 or segments[2] != "blob":
            raise HTTPException(
                status_code=400,
                detail=(
                    "skill_url must be a "
                    "'<host>/<owner>/<repo>/blob/<ref>/<path>/SKILL.md' URL"
                ),
            )
        owner, repo, _blob, ref, *rest = segments
        skill_path = "/".join(rest)
        if skill_path.endswith("SKILL.md"):
            skill_path = skill_path.rsplit("/", 1)[0] if "/" in skill_path else ""
        git_url = f"{parsed.scheme}://{parsed.netloc}/{owner}/{repo}"
        return git_url, ref, skill_path

    git_url = payload.repo_url.strip()
    ref = (payload.branch or "").strip()
    return git_url, ref, ""


@router.get("/telemetry")
@limiter.limit("10/minute")
async def receive_telemetry(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Receive telemetry data from skillhub CLI and update counters for installs.

    Matched skills also get a download_history record (with IP/User-Agent),
    which feeds the weekly/monthly download ranking.

    request params:
    {
        'v': '1.5.13',
        'event': 'install',
        'source': 'vercel-labs/agent-skills',
        'sourceType': 'github',
        'skills': 'deploy-to-vercel',
        'agents': 'amp,antigravity,antigravity-cli,cline,codex,cursor,deepagents,gemini-cli,github-copilot,kimi-code-cli,opencode,warp,zed,openclaw',
        'skillFiles': '{"deploy-to-vercel":"skills/deploy-to-vercel/SKILL.md"}'
    }
    """
    params = dict(request.query_params)
    telemetry_service = TelemetryService(db)
    matched_skill_ids = await telemetry_service.process(
        params,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return {"ok": True, "matched_skill_ids": matched_skill_ids}


def skill_to_response(skill) -> SkillResponse:
    return SkillResponse(
        id=str(skill.id),
        skill_id=skill.skill_id,
        name=skill.name,
        description=skill.description,
        version=skill.version,
        commit_id=skill.commit_id,
        author=skill.author,
        source=skill.source,
        source_url=skill.source_url,
        repo_url=skill.repo_url,
        category=skill.category,
        category_label=category_label(skill.category),
        tags=skill.tags,
        platform=skill.platform,
        metadata=skill.extra_metadata,
        content=skill.content,
        risk_score=skill.risk_score,
        download_count=skill.download_count,
        period_downloads=getattr(skill, "_period_downloads", None),
        rating=skill.rating,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
        last_indexed_at=skill.last_indexed_at,
    )


@router.get("/", response_model=SkillListResponse)
async def list_skills(
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    category: Annotated[str | None, Query(max_length=1000)] = None,
    platform: Annotated[str | None, Query(max_length=500)] = None,
    tags: Annotated[str | None, Query(max_length=2000)] = None,
    security_level: Annotated[str | None, Query(max_length=500)] = None,
    source_type: Annotated[str | None, Query(max_length=50)] = None,
    repo: Annotated[str | None, Query(max_length=500)] = None,
    sort_by: Annotated[str, Query(pattern="^(updated_at|download_count)$")] = "updated_at",
    sort_period: Annotated[str | None, Query(pattern="^(week|month)$")] = None,
    db: AsyncSession = Depends(get_db),
):
    tag_list = tags.split(",") if tags else None
    category_list = category.split(",") if category else None
    platform_list = platform.split(",") if platform else None
    security_level_list = security_level.split(",") if security_level else None
    # repo 过滤：匹配 skill_id 前缀 {source_type}:{owner}/{repo}/，
    # 与 source_type 过滤（Skill.source 列）一起限定到具体仓库
    skill_id_prefix = (
        f"{source_type.strip()}:{repo.strip()}"
        if source_type and source_type.strip() and repo and repo.strip()
        else None
    )
    repo = SkillRepository(db)
    skills, total = await repo.list(
        skip=skip,
        limit=limit,
        category=category_list,
        platform=platform_list,
        tags=tag_list,
        security_level=security_level_list,
        source=source_type,
        skill_id_prefix=skill_id_prefix,
        sort_by=sort_by,
        sort_period=sort_period,
    )

    return SkillListResponse(
        skills=[skill_to_response(s) for s in skills],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/{skill_id:path}/audit", response_model=SecurityAuditResponse | ErrorResponse)
async def audit_skill(
    skill_id: SkillIdPath,
    db: AsyncSession = Depends(get_db),
):
    repo = SkillRepository(db)
    skill = await repo.get_by_skill_id(skill_id)

    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    security_service = SecurityService(db)
    audit_repo = security_service.audit_repo
    latest_audit = await audit_repo.get_latest_by_resource("skill", skill.id)

    if latest_audit:
        return SecurityAuditResponse(
            id=str(latest_audit.id),
            resource_type=latest_audit.resource_type,
            resource_id=str(latest_audit.resource_id),
            version=latest_audit.version,
            commit_id=latest_audit.commit_id,
            audit_type=latest_audit.audit_type,
            risk_level=latest_audit.risk_level,
            risk_score=skill.risk_score,
            risk_signals=latest_audit.risk_signals,
            details=latest_audit.details,
            audited_at=latest_audit.audited_at,
        )

    return {"error": "No audit found"}


@router.get(
    "/{skill_id:path}/audit-report",
    response_model=AuditReportResponse,
)
async def get_skill_audit_report(
    skill_id: SkillIdPath,
    db: AsyncSession = Depends(get_db),
):
    """Return the aggregated risk assessment report for a skill.

    Read-only aggregation on top of ``GET /{skill_id}/audit``: re-derives the four
    risk categories, severity stats and per-finding rows from the SkillSpector
    report stored in ``security_audits.details``.  When no audit row or no parsed
    report exists, ``has_report`` is false and ``reason`` explains why; unknown
    skills still return 404.
    """
    repo = SkillRepository(db)
    skill = await repo.get_by_skill_id(skill_id)

    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    security_service = SecurityService(db)
    latest_audit = await security_service.audit_repo.get_latest_by_resource("skill", skill.id)

    return AuditReportResponse.model_validate(build_audit_report(skill, latest_audit))


@router.post(
    "/{skill_id:path}/audit",
    response_model=SecurityAuditResponse | ErrorResponse,
    dependencies=[Depends(require_admin_token)],
)
async def trigger_skill_audit(
    skill_id: SkillIdPath,
    scanners: str | None = Query(None, description="Comma-separated: skillspector"),
    async_mode: bool = Query(False, description="Trigger scan without waiting for result"),
    db: AsyncSession = Depends(get_db),
):
    """Trigger a fresh security audit for a skill and return the result."""
    repo = SkillRepository(db)
    skill = await repo.get_by_skill_id(skill_id)

    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    security_service = SecurityService(db)
    if not security_service.detector.enable_audit:
        raise HTTPException(status_code=503, detail="Security audit is disabled")

    scanner_list = None
    if scanners:
        scanner_list = [s.strip() for s in scanners.split(",") if s.strip()]

    audit_result = await security_service.audit_skill(
        skill_id=skill.skill_id,
        source=skill.source,
        # Jenkins 需要合法 git 仓库 URL（source_url 是 SKILL.md 的 blob 链接，不可用于 clone）
        source_url=skill.repo_url or skill.source_url,
        metadata={
            # Jenkins 需要真实 git ref；version 可能是 "latest"，用 commit_id 兜底
            "version": skill.commit_id or skill.version,
            "commit_id": skill.commit_id,
            "content": skill.content,
            "skill_path": _derive_scan_skill_path(
                skill.source, skill.source_url, skill.skill_id
            ),
        },
        scanners=scanner_list,
        async_mode=async_mode,
    )

    if "error" in audit_result:
        return {"error": audit_result["error"]}

    # Fetch the newly created audit record
    audit_repo = security_service.audit_repo
    latest_audit = await audit_repo.get_latest_by_resource("skill", skill.id)

    if not latest_audit:
        return {"error": "Audit completed but no record found"}

    return SecurityAuditResponse(
        id=str(latest_audit.id),
        resource_type=latest_audit.resource_type,
        resource_id=str(latest_audit.resource_id),
        version=latest_audit.version,
        commit_id=latest_audit.commit_id,
        audit_type=latest_audit.audit_type,
        risk_level=latest_audit.risk_level,
        risk_signals=latest_audit.risk_signals,
        details=latest_audit.details,
        audited_at=latest_audit.audited_at,
    )


@router.post(
    "/audit-by-url",
    response_model=AuditByUrlResponse | ErrorResponse,
    dependencies=[Depends(require_admin_token)],
)
async def audit_by_url(
    payload: AuditByUrlRequest,
    db: AsyncSession = Depends(get_db),
):
    """Run a one-off security audit for a git URL without registering the skill.

    Used by the openEuler-skills PR gate: audit a skill repository URL
    (whole repo) or a SKILL.md URL (single skill) before the content is
    merged into the skill index.  Nothing is persisted to the database.

    The request must provide either ``repo_url`` (with optional ``branch``)
    or ``skill_url`` (a ``.../blob/<ref>/<path>/SKILL.md`` link).
    """
    if not payload.repo_url and not payload.skill_url:
        raise HTTPException(
            status_code=422, detail="Either repo_url or skill_url is required"
        )

    security_service = SecurityService(db)
    if not security_service.detector.enable_audit:
        raise HTTPException(status_code=503, detail="Security audit is disabled")

    # 1) 纯解析目标（无网络），先校验 URL 再发起任何网络访问，避免 SSRF
    git_url, ref, skill_path = _derive_audit_target(payload)

    is_valid, error_msg = validate_git_url(git_url)
    if not is_valid:
        raise HTTPException(
            status_code=400, detail=f"Invalid git URL: {error_msg}"
        )

    # 2) repo 类型未显式指定 branch 时，URL 校验通过后再解析默认分支；
    #    git ls-remote 是阻塞子进程，放线程池避免阻塞事件循环
    if not ref:
        ref = await asyncio.to_thread(_resolve_default_branch, git_url)

    scanner_list = None
    if payload.scanners:
        scanner_list = [s.strip() for s in payload.scanners.split(",") if s.strip()]

    result = await security_service.audit_external(
        git_url=git_url,
        ref=ref,
        skill_path=skill_path,
        scanners=scanner_list,
        async_mode=payload.async_mode,
    )
    return AuditByUrlResponse(**result)


@router.get(
    "/audit-by-url/result",
    response_model=AuditByUrlResultResponse | ErrorResponse,
    dependencies=[Depends(require_admin_token)],
)
async def audit_by_url_result(
    build_number: Annotated[int, Query(ge=1, description="Jenkins build number")],
    db: AsyncSession = Depends(get_db),
):
    """Poll the result of an async audit-by-URL scan (async_mode=True).

    The openEuler-skills PR gate triggers a scan with ``async_mode=True``
    (which returns immediately with ``details.skillspector_build_number``),
    then polls this endpoint until ``status == "done"``.  Returns
    ``pending`` while the Jenkins build is still running, ``done`` with the
    audit result once the report is collected, or ``error``.
    """
    security_service = SecurityService(db)
    if not security_service.detector.enable_audit:
        raise HTTPException(status_code=503, detail="Security audit is disabled")
    result = await security_service.get_external_result(build_number)
    return AuditByUrlResultResponse(**result)


@router.get(
    "/audit-by-url/report",
    response_model=None,
)
async def audit_by_url_report(
    build_number: Annotated[int, Query(ge=1, description="Jenkins build number")],
    filename: Annotated[
        str | None, Query(max_length=2048, description="Custom download filename")
    ] = None,
    db: AsyncSession = Depends(get_db),
):
    """Serve report.md for a one-off audit-by-URL scan as a downloadable file.

    Clicked from the openEuler-skills PR gate comment (detail link).  The
    report content is fetched from the Jenkins artifact on demand, so it
    stays fresh after the scan.  Read-only and intentionally unauthenticated
    so the link can be opened directly in a browser.
    """
    security_service = SecurityService(db)
    if not security_service.detector.enable_audit:
        raise HTTPException(status_code=503, detail="Security audit is disabled")
    md = await security_service.get_external_report_md(build_number)
    if md is None:
        raise HTTPException(
            status_code=404, detail="report.md not found for build"
        )
    # 下载文件名：优先使用门禁传入的 filename（skill 名 + 安全审计报告.md），
    # 否则回退为 report-<build_number>.md；剔除可能破坏 Content-Disposition 的字符。
    # Content-Disposition 只允许 latin-1，中文等非 ASCII 文件名需用 RFC 5987 的
    # filename*=UTF-8''<percent-encoded> 携带，ASCII 名做兼容兜底。
    safe_name = re.sub(r'["\r\n]', "", filename or "").strip()
    if not safe_name:
        disposition = 'attachment; filename="report-{}.md"'.format(build_number)
    else:
        try:
            safe_name.encode("latin-1")
        except UnicodeEncodeError:
            disposition = (
                'attachment; filename="report-{}.md"; filename*=UTF-8\'\'{}'.format(
                    build_number, quote(safe_name, safe="")
                )
            )
        else:
            disposition = 'attachment; filename="{}"'.format(safe_name)
    return Response(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": disposition},
    )


@router.get("/contributors/{source}/{author}", response_model=ContributorSkillsResponse)
async def get_contributor_skills(
    source: Annotated[str, Path(max_length=50, description="平台来源：github / atomgit")],
    author: Annotated[str, Path(max_length=255, description="contributor 名称（skills.author 精确匹配）")],
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    sort_by: Annotated[str, Query(pattern="^(updated_at|download_count)$")] = "download_count",
    keyword: Annotated[str | None, Query(max_length=200)] = None,
    db: AsyncSession = Depends(get_db),
):
    """获取 contributor（source + author）名下全部可见 skills 的聚合视图。

    platform/description/repo_url 取自 contributors 表（扫描时写入），
    skill 列表 + total_downloads 从 skills 表实时查询。
    keyword 按 name/description/tags 过滤（服务端分页），total 随之变化；
    skill_count / total_downloads 保持全量口径不随搜索变化。
    """
    contributor_repo = ContributorRepository(db)
    contributor = await contributor_repo.get(source, author)
    if not contributor:
        raise HTTPException(status_code=404, detail="Contributor not found")

    skill_repo = SkillRepository(db)
    skills, total, total_downloads, _, skill_count = (
        await skill_repo.get_contributor_skills(
            source, author, skip=skip, limit=limit, sort_by=sort_by, keyword=keyword
        )
    )
    if not skill_count:
        raise HTTPException(status_code=404, detail="Contributor has no visible skills")

    return ContributorSkillsResponse(
        source=source,
        author=author,
        platform=contributor.platform,
        name=contributor.name,
        description=contributor.description,
        git_profile=contributor.git_profile,
        website=contributor.website,
        skill_count=skill_count,
        total_downloads=total_downloads,
        skills=[skill_to_response(s) for s in skills],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/contributors", response_model=ContributorListResponse)
async def list_contributors(
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    platform: Annotated[str | None, Query(max_length=100)] = None,
    keyword: Annotated[str | None, Query(max_length=200)] = None,
    sort_by: Annotated[str, Query(pattern="^(skill_count|created_at)$")] = "skill_count",
    db: AsyncSession = Depends(get_db),
):
    """列出所有贡献者，支持按 platform/keyword 筛选、按 skill 数量或注册时间排序。

    ``platform_counts`` 返回当前 keyword 下各 platform 的命中数量（忽略 platform
    筛选），前端标签页展示数量时使用。
    """
    repo = ContributorRepository(db)
    rows, total, platform_counts, grand_total = await repo.list(
        skip=skip,
        limit=limit,
        platform=platform,
        keyword=keyword,
        sort_by=sort_by,
    )
    return ContributorListResponse(
        contributors=[
            ContributorResponse(
                id=str(c.id),
                source=c.source,
                author=c.author,
                platform=c.platform,
                name=c.name,
                description=c.description,
                avatar_url=c.avatar_url,
                git_profile=c.git_profile,
                website=c.website,
                repo_url=c.repo_url,
                skill_count=c.skill_count,
                total_downloads=downloads,
                created_at=c.created_at,
                updated_at=c.updated_at,
            )
            for c, downloads in rows
        ],
        total=total,
        grand_total=grand_total,
        skip=skip,
        limit=limit,
        platform_counts=platform_counts,
    )


@router.get("/versions/{skill_id:path}", response_model=SkillVersionsResponse)
async def get_skill_versions(
    skill_id: SkillIdPath,
    db: AsyncSession = Depends(get_db),
):
    skill_repo = SkillRepository(db)
    latest_skill = await skill_repo.get_by_skill_id(skill_id)
    tagged_versions = await skill_repo.get_versions_by_base_skill(None, skill_id)

    if not latest_skill and not tagged_versions:
        raise HTTPException(status_code=404, detail="Skill not found")

    # 只返回 skill_versions 表的历史版本（Tag 版本），不包含 skills 表的 latest 行
    versions = list(tagged_versions) if tagged_versions else []
    source_url = (
        latest_skill.source_url
        if latest_skill
        else (versions[0].source_url if versions else "")
    )

    return SkillVersionsResponse(
        source_url=source_url,
        skill_id=skill_id,
        versions=[skill_to_response(s) for s in versions],
    )


@router.get(
    "/{skill_id:path}/download",
    response_class=FileResponse,
    responses={
        200: {
            "description": "Packaged Skill ZIP",
            "content": {"application/zip": {}},
        },
        404: {"description": "Skill not found"},
        409: {"description": "Local Skill repository is unavailable"},
    },
)
async def download_skill(
    skill_id: SkillIdPath,
    request: Request,
    version: Annotated[str | None, Query(description="指定版本号，从 skill_versions 表下载对应版本")] = None,
    db: AsyncSession = Depends(get_db),
):
    repo = SkillRepository(db)

    if version:
        skill = await repo.get_version_with_repository(skill_id, version)
        if not skill:
            raise HTTPException(status_code=404, detail="Skill version not found")
        resource_type = "skill_version"
    else:
        skill = await repo.get_with_repository_by_skill_id(skill_id)
        if not skill:
            raise HTTPException(status_code=404, detail="Skill not found")
        resource_type = "skill"

    if not skill.skill_repo:
        raise HTTPException(status_code=409, detail="Skill repository metadata is missing")

    download_manager = DownloadManager()
    try:
        archive = await download_manager.create_skill_archive(
            skill=skill,
            repository=skill.skill_repo,
        )
    except SkillArchiveNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SkillArchiveConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SkillArchiveError as exc:
        raise HTTPException(status_code=500, detail="Failed to package Skill") from exc

    dl_history = DownloadHistoryRepository(db)
    await dl_history.create({
        "resource_type": resource_type,
        "resource_id": skill.id,
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    })
    await repo.increment_download(skill_id)
    await db.commit()

    return FileResponse(
        path=archive.path,
        filename=archive.filename,
        media_type=archive.media_type,
    )


@router.get("/{skill_id:path}", response_model=SkillResponse | ErrorResponse)
async def get_skill(skill_id: SkillIdPath, db: AsyncSession = Depends(get_db)):
    repo = SkillRepository(db)
    skill = await repo.get_by_skill_id(skill_id)

    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    return skill_to_response(skill)


@router.post(
    "/",
    response_model=SkillResponse,
    status_code=201,
    dependencies=[Depends(require_admin_token)],
)
async def create_skill(
    skill_data: SkillCreate,
    request: Request,
    async_mode: bool = Query(False, description="Trigger security audit without waiting for result"),
    db: AsyncSession = Depends(get_db),
):
    # Validate source_url for SSRF protection
    from src.security.detector import validate_git_url
    if skill_data.source_url:
        is_valid, error_msg = validate_git_url(skill_data.source_url)
        if not is_valid:
            raise HTTPException(status_code=400, detail=f"Invalid source URL: {error_msg}")

    repo = SkillRepository(db)

    existing = await repo.get_by_skill_id(skill_data.skill_id)
    if existing:
        raise HTTPException(status_code=409, detail="Skill already exists")

    skill_dict = skill_data.model_dump()

    # Resolve skill_repo_id: look up or create the source repository
    if not skill_dict.get("skill_repo_id"):
        repo_repo = SkillRepoRepository(db)
        source = skill_data.source
        source_url = skill_data.source_url
        repo_name = source_url
        for prefix in ("https://", "http://"):
            if repo_name.startswith(prefix):
                repo_name = repo_name[len(prefix):]
        repo_name = repo_name.removesuffix(".git").replace("/", "_")
        existing_repo = await repo_repo.get_skill_repository_by_repo_name(repo_name)
        if existing_repo:
            skill_dict["skill_repo_id"] = existing_repo.id
        else:
            new_repo = await repo_repo.create_skill_repository(
                repo_name=repo_name,
                source=source,
                branch=skill_data.version or "main",
                url=source_url,
                local_path=None,
                skill_discover_status="init",
                platform=skill_data.platform,
            )
            skill_dict["skill_repo_id"] = new_repo.id

    skill = await repo.create(skill_dict)

    # Run security audit after skill is persisted, then write back the score
    security_service = SecurityService(db)
    if security_service.detector.enable_audit:
        try:
            await security_service.audit_skill(
                skill.skill_id,
                skill.source,
                skill.repo_url or skill.source_url,
                {
                    "version": skill.commit_id or skill.version,
                    "commit_id": skill.commit_id,
                    "content": skill.content or "",
                    "skill_path": _derive_scan_skill_path(
                        skill.source, skill.source_url, skill.skill_id
                    ),
                },
                async_mode=async_mode,
            )
            # risk_score is already persisted by audit_skill internally
        except Exception:
            _logger.warning("Audit failed for skill %s", skill.skill_id, exc_info=True)

    return skill_to_response(skill)


@router.delete(
    "/{skill_id:path}",
    dependencies=[Depends(require_admin_token)],
)
async def delete_skill(skill_id: SkillIdPath, db: AsyncSession = Depends(get_db)):
    repo = SkillRepository(db)
    deleted = await repo.delete(skill_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Skill not found")

    return {"message": "Skill deleted", "skill_id": skill_id}
