"""Security detection engine — unified entry point for all audit scanners.

Components
----------
* Skillspector       — Jenkins‑based deep code audit (sync + async)
"""
import asyncio
import ipaddress
import json
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings

settings = get_settings()


class _SecurityLogAdapter(logging.LoggerAdapter):
    """给本模块日志统一加 `[域名][子域]` 前缀，区分 collector 与检测功能两类目标。"""

    def __init__(self, logger: logging.Logger, component: str) -> None:
        super().__init__(logger, {})
        self._component = component

    def process(self, msg: Any, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        return f"{self._component} {msg}", kwargs


logger: logging.LoggerAdapter[None] = _SecurityLogAdapter(
    logging.getLogger(__name__), "[Security][Detector]"
)
collector_logger: logging.LoggerAdapter[None] = _SecurityLogAdapter(
    logging.getLogger(__name__), "[Security][Collector]"
)

ALLOWED_GIT_HOSTS = frozenset({
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "gitea.io",
    "gitee.com",
    "atomgit.com",
    "codeberg.org",
    "git.sr.ht",
})
ALLOWED_GIT_SCHEMES = frozenset({"http", "https", "git", "ssh"})
MAX_GIT_URL_LENGTH = 2048


def validate_git_url(url: str) -> tuple[bool, str]:
    """Validate a git URL for SSRF protection.

    Returns
    -------
    tuple[bool, str]
        (is_valid, error_message)
    """
    if not isinstance(url, str) or not url.strip():
        return False, "URL cannot be empty"

    url = url.strip()
    if len(url) > MAX_GIT_URL_LENGTH:
        return False, "URL is too long"
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in url):
        return False, "URL contains control characters"

    # 1. Parse URL
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as e:
        return False, f"Invalid URL format: {str(e)}"

    # 2. Protocol validation
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_GIT_SCHEMES:
        return False, f"Unsupported protocol: {parsed.scheme}"

    if parsed.fragment:
        return False, "URL fragments are not allowed"
    if scheme in {"http", "https", "git"} and (parsed.username or parsed.password):
        return False, "Credentials in URL are not allowed"
    if scheme == "ssh" and parsed.password:
        return False, "Passwords in URL are not allowed"

    # 3. Extract hostname
    try:
        hostname = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return False, "Invalid hostname or port"

    if not hostname:
        return False, "Could not extract hostname from URL"

    allowed_ports = {
        "http": {None, 80},
        "https": {None, 443},
        "git": {None, 9418},
        "ssh": {None, 22},
    }
    if port not in allowed_ports[scheme]:
        return False, f"Non-standard port is not allowed: {port}"

    # 4. Private/loopback IP check
    try:
        ip_obj = ipaddress.ip_address(hostname)
        if not ip_obj.is_global:
            return False, f"Non-public IP addresses are not allowed: {hostname}"
    except ValueError:
        # It's a domain name, not an IP - continue
        pass

    # 5. Domain whitelist
    # Exact host matching avoids trusting arbitrary subdomains of a provider.
    if hostname not in ALLOWED_GIT_HOSTS:
        return False, f"Domain not in whitelist: {hostname}"

    path = urllib.parse.unquote(parsed.path)
    if not path or path == "/":
        return False, "Repository path is required"
    if "\\" in path or any(part in {".", ".."} for part in path.split("/")):
        return False, "Invalid repository path"

    return True, ""


# ═══════════════════════════════════════════════════════════════════════════
# Shared data structures
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class RiskSignal:
    id: str
    name: str
    description: str
    severity: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class SecurityReport:
    resource_type: str
    resource_id: str
    risk_level: str
    risk_signals: list[RiskSignal]
    details: dict[str, Any] = field(default_factory=dict)


def _sanitize_json_value(value: Any) -> Any:
    """Replace NUL characters recursively before storing values in JSONB."""
    if isinstance(value, str):
        return value.replace("\x00", "\\0")
    if isinstance(value, dict):
        return {
            _sanitize_json_value(key): _sanitize_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_json_value(item) for item in value]
    return value


def _log_skillspector_final_result(
    *,
    audit_id: Any,
    build_number: int,
    status: str,
    outcome: str,
    risk_level: str | None = None,
    score: Any = None,
    signal_count: int = 0,
    error: str | None = None,
) -> None:
    """Write one searchable JSON log entry for every terminal scan result."""
    result = {
        "event": "skillspector_final_result",
        "audit_id": str(audit_id),
        "build_number": build_number,
        "jenkins_status": status,
        "outcome": outcome,
        "risk_level": risk_level,
        "score": score,
        "signal_count": signal_count,
        "error": error,
    }
    collector_logger.info(
        "Skillspector final result: %s",
        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
    )


def _log_collector_event(event: str, **fields: Any) -> None:
    """Write one structured trace entry for an async collector decision."""
    payload = {"event": event, **fields}
    collector_logger.info(
        "%s",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Skillspector — Jenkins‑based deep scanner
# ═══════════════════════════════════════════════════════════════════════════


class SkillspectorClient:
    """Client for the skill-scanner Jenkins job that runs Skillspector.

    The Jenkins job accepts parameters:
        GIT_URL, REF, SKILL_PATH, SCANNERS

    On success the build produces:
        reports/skillspector/report.json
    """

    JOB_PATH = "/job/skill-scanner"

    def __init__(
        self,
        jenkins_url: str,
        user: str = "",
        token: str = "",
        timeout: float = 600.0,
        poll_interval: float = 1.0,
    ):
        self.base_url = jenkins_url.rstrip("/")
        self.auth = (user, token) if user and token else None
        self.timeout = timeout
        self.poll_interval = poll_interval

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when the client has credentials and a valid URL."""
        return self.auth is not None and bool(self.base_url)

    def run_scan(
        self,
        git_url: str,
        ref: str = "main",
        skill_path: str = "",
        scanners: str = "skillspector",
    ) -> tuple[dict[str, Any], str | None]:
        """Convenience: trigger a build, wait for it, and return the report.

        Returns a ``(report, report_md)`` tuple.  ``report_md`` is the raw
        Markdown report artifact (or *None* if unavailable).  A build may
        finish with a non-SUCCESS status (e.g. skillspector reports a critical
        finding and the pipeline exits non-zero) while still producing
        report.json.  We therefore treat the report artifact as the source of
        truth and only error out when it cannot be fetched.
        """
        build_number = self.trigger_scan(git_url, ref, skill_path, scanners)
        if build_number is None:
            return {"error": "Failed to trigger Jenkins build"}, None
        status = self.wait_for_build(build_number)
        if status is None:
            return {"error": f"Build {build_number} did not finish within the timeout"}, None
        report = self.fetch_report(build_number)
        if report is None:
            return {"error": f"Failed to fetch report for build {build_number} (status {status})"}, None
        return report, self.fetch_report_md(build_number)

    # ------------------------------------------------------------------
    # Low-level Jenkins API
    # ------------------------------------------------------------------

    def _get_crumb(self, client: httpx.Client) -> tuple[str, str] | None:
        try:
            resp = client.get(
                f"{self.base_url}/crumbIssuer/api/json", auth=self.auth
            )
            if resp.status_code == 200:
                data = resp.json()
                return (
                    data.get("crumbRequestField", "Jenkins-Crumb"),
                    data.get("crumb", ""),
                )
        except httpx.RequestError:
            pass
        return None

    def trigger_scan(
        self,
        git_url: str,
        ref: str = "main",
        skill_path: str = "",
        scanners: str = "skillspector",
    ) -> int | None:
        """Trigger a build via buildWithParameters; return the build number."""
        # Validate git URL for SSRF protection
        is_valid, error_msg = validate_git_url(git_url)
        if not is_valid:
            logger.error("Git URL validation failed: %s", error_msg)
            return None

        trigger_started_at = time.perf_counter()
        url = f"{self.base_url}{self.JOB_PATH}/buildWithParameters"
        params = {
            "GIT_URL": git_url,
            "REF": ref,
            "SKILL_PATH": skill_path,
            "SCANNERS": scanners,
        }

        with httpx.Client(timeout=30.0) as client:
            headers: dict[str, str] = {}
            crumb_started_at = time.perf_counter()
            crumb = self._get_crumb(client)
            crumb_elapsed = time.perf_counter() - crumb_started_at
            if crumb is not None:
                headers[crumb[0]] = crumb[1]

            submit_started_at = time.perf_counter()
            try:
                resp = client.post(url, data=params, auth=self.auth, headers=headers)
            except httpx.RequestError as exc:
                logger.error(
                    "Failed to reach Jenkins after %.3fs: %s",
                    time.perf_counter() - trigger_started_at,
                    exc,
                )
                return None
            submit_elapsed = time.perf_counter() - submit_started_at

        if resp.status_code not in (200, 201, 302, 303):
            logger.error(
                "Jenkins trigger returned %d: %s", resp.status_code, resp.text[:500]
            )
            return None

        location = resp.headers.get("Location", "")
        if not location:
            logger.warning("No Location header in Jenkins response")
            return None

        queue_started_at = time.perf_counter()
        build_number = self._resolve_queue_item(location)
        queue_elapsed = time.perf_counter() - queue_started_at
        logger.debug(
            "Jenkins trigger timing: build_number=%s crumb=%.3fs submit=%.3fs "
            "queue=%.3fs total=%.3fs",
            build_number,
            crumb_elapsed,
            submit_elapsed,
            queue_elapsed,
            time.perf_counter() - trigger_started_at,
        )
        return build_number

    def wait_for_build(
        self,
        build_number: int,
        max_wait: float | None = None,
    ) -> str | None:
        """Poll build status until it finishes.  Returns SUCCESS/FAILURE/ABORTED/UNSTABLE/None."""
        max_wait = max_wait if max_wait is not None else self.timeout
        deadline = time.monotonic() + max_wait

        while time.monotonic() < deadline:
            status = self.get_build_status(build_number)
            if status not in (None, "BUILDING"):
                return status
            if status is None:
                logger.warning("Build #%d status unavailable; retrying", build_number)
            time.sleep(self.poll_interval)

        logger.error("Build %d did not finish within %.0f s", build_number, max_wait)
        return None

    def get_build_status(self, build_number: int) -> str | None:
        """Fetch build state once without waiting.

        Returns ``BUILDING`` while Jenkins is running the build, a terminal
        Jenkins result when complete, and ``None`` for transient lookup errors.
        """
        url = f"{self.base_url}{self.JOB_PATH}/{build_number}/api/json"
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url, auth=self.auth)
        except httpx.RequestError as exc:
            logger.warning("Failed to fetch status for build #%d: %s", build_number, exc)
            return None

        if resp.status_code != 200:
            if resp.status_code == 404:
                return "NOT_FOUND"
            logger.warning(
                "Build status fetch failed: build_number=%s status_code=%s",
                build_number,
                resp.status_code,
            )
            return None

        try:
            data = resp.json()
        except ValueError:
            logger.warning("Invalid JSON in status response for build #%d", build_number)
            return None

        if data.get("building", False):
            return "BUILDING"
        result = data.get("result")
        return str(result) if result is not None else None

    def fetch_report(self, build_number: int) -> dict[str, Any] | None:
        """Fetch the JSON report artifact from a completed build."""
        report, _ = self.fetch_report_with_status(build_number)
        return report

    def fetch_report_md(self, build_number: int) -> str | None:
        """Fetch the Markdown report artifact (report.md) from a completed build."""
        url = (
            f"{self.base_url}{self.JOB_PATH}/{build_number}"
            "/artifact/reports/skillspector/report.md"
        )
        with httpx.Client(timeout=30.0) as client:
            try:
                resp = client.get(url, auth=self.auth)
            except httpx.RequestError as exc:
                logger.error("Failed to fetch report.md: %s", exc)
                return None

        if resp.status_code != 200:
            logger.warning(
                "Report.md fetch failed: build_number=%s status_code=%s url=%s",
                build_number,
                resp.status_code,
                url,
            )
            return None
        return resp.text

    def fetch_report_with_status(
        self, build_number: int,
    ) -> tuple[dict[str, Any] | None, int | None]:
        """Fetch a report and expose HTTP status for retry decisions."""
        url = (
            f"{self.base_url}{self.JOB_PATH}/{build_number}"
            "/artifact/reports/skillspector/report.json"
        )
        with httpx.Client(timeout=30.0) as client:
            try:
                resp = client.get(url, auth=self.auth)
            except httpx.RequestError as exc:
                logger.error("Failed to fetch report: %s", exc)
                return None, None

        if resp.status_code != 200:
            logger.warning(
                "Report fetch failed: build_number=%s status_code=%s url=%s",
                build_number,
                resp.status_code,
                url,
            )
            return None, resp.status_code

        try:
            return resp.json(), resp.status_code
        except ValueError as exc:
            logger.error("Invalid JSON in report: %s", exc)
            return None, resp.status_code

    # ------------------------------------------------------------------
    # Report parsing
    # ------------------------------------------------------------------

    @staticmethod
    def report_to_risk_signals(report: dict[str, Any]) -> list[RiskSignal]:
        """Convert a skillspector report dict into RiskSignal objects."""
        signals: list[RiskSignal] = []

        issues = report.get("issues", []) or []
        for issue in issues:
            issue_id = issue.get("id", "unknown")
            severity = issue.get("severity", "UNKNOWN").upper()
            explanation = issue.get("explanation", issue.get("finding", ""))
            remediation = issue.get("remediation", "")
            confidence = issue.get("confidence")
            location = issue.get("location", {})

            loc_str = ""
            if isinstance(location, dict):
                fname = location.get("file", "")
                line = location.get("start_line", "")
                if fname:
                    loc_str = f" ({fname}" + (f":{line})" if line else ")")
            name = f"Skillspector {issue_id}{loc_str}"

            signals.append(
                RiskSignal(
                    id=issue_id,
                    name=name,
                    description=explanation,
                    severity=severity,
                    data={
                        "remediation": remediation,
                        "confidence": confidence,
                        "location": location,
                        "source": "skillspector",
                    },
                )
            )

        return signals

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_queue_item(self, location: str) -> int | None:
        """Poll a Jenkins queue item URL until a build number is assigned."""
        if location.startswith("http"):
            url = f"{location.rstrip('/')}/api/json"
        else:
            url = f"{self.base_url}{location.rstrip('/')}/api/json"

        deadline = time.monotonic() + 60.0

        with httpx.Client(timeout=10.0) as client:
            while time.monotonic() < deadline:
                try:
                    resp = client.get(url, auth=self.auth)
                except httpx.RequestError:
                    time.sleep(self.poll_interval)
                    continue

                if resp.status_code != 200:
                    time.sleep(self.poll_interval)
                    continue

                data = resp.json()
                executable = data.get("executable")
                if executable and isinstance(executable, dict):
                    return executable.get("number")

                if data.get("cancelled") or data.get("stuck"):
                    logger.error("Queue item stuck/cancelled: %s", data)
                    return None

                time.sleep(self.poll_interval)

        logger.error("Queue item did not resolve within 60 s")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Background collector — polls Jenkins for async scan results
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_POLL_INTERVAL_SECONDS = 10
MAX_REPORT_FETCH_ATTEMPTS = 3


def _record_unavailable_report_attempt(
    details: dict[str, Any] | None,
    status: str,
    *,
    terminal: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Record one missing-report attempt and finalize after the retry limit."""
    updated = dict(details or {})
    raw_attempts = updated.get("skillspector_report_fetch_attempts", 0)
    try:
        attempts = int(raw_attempts) + 1
    except (TypeError, ValueError):
        attempts = 1

    exhausted = terminal or attempts >= MAX_REPORT_FETCH_ATTEMPTS
    updated["skillspector_report_fetch_attempts"] = attempts
    updated["skillspector_status"] = status
    if exhausted:
        updated["skillspector_collected"] = True
        updated["skillspector_error"] = "report_unavailable"

    return updated, exhausted


MAX_DETAILS_BYTES = 1024 * 1024  # details 估算大小阈值（1MB），超过才截断
SNIPPET_MAX_CHARS = 500          # 截断后单条 code_snippet 上限


def _maybe_compress_details(details: dict[str, Any]) -> dict[str, Any]:
    """details 估算大小超过阈值时才截断 code_snippet，小报告保持原样。

    估算使用与 PG JSONB 存储同量级的序列化大小（不做 TOAST 压缩的精确模拟，
    仅用于阈值决策）。小报告（≤1MB）完全原样保留，不影响前端完整展示；只对
    异常大的记录瘦身，把触发面控制在极小范围。
    """
    if len(json.dumps(details, ensure_ascii=False).encode("utf-8")) <= MAX_DETAILS_BYTES:
        return details

    report = details.get("skillspector_report")
    if not isinstance(report, dict):
        return details
    for issue in report.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        snippet = issue.get("code_snippet")
        if isinstance(snippet, str) and len(snippet) > SNIPPET_MAX_CHARS:
            issue["code_snippet"] = snippet[:SNIPPET_MAX_CHARS] + (
                f"\n... [truncated, original {len(snippet)} chars]"
            )
            issue["truncated"] = True
    return details


class SkillspectorCollector:
    """Polls Jenkins for completed async scans and updates DB records."""

    def __init__(
        self,
        client: SkillspectorClient,
        session_factory: Any,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ):
        self._client = client
        self._session_factory = session_factory
        self._poll_interval = poll_interval
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        collector_logger.info("SkillspectorCollector started (poll every %ds)", self._poll_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        collector_logger.info("SkillspectorCollector stopped")

    async def collect_once(self) -> int:
        """Scan for pending async audits and collect completed results.

        Returns the number of audits whose results were successfully collected.
        """
        processed = 0
        async with self._session_factory() as session:
            pending = await self._fetch_pending(session)
            _log_collector_event(
                "batch_started",
                pending_count=len(pending),
                batch_limit=100,
            )
            # Snapshot every scalar field before the loop. A failed item rolls
            # back the session, which expires all ORM instances; reading
            # ``audit.details`` on the next item after the rollback would then
            # attempt async lazy loading outside a greenlet (MissingGreenlet)
            # and break failure isolation for the whole batch.
            snapshots = [
                (
                    audit.id,
                    audit.resource_type,
                    audit.resource_id,
                    dict(audit.details or {}),
                    list(audit.risk_signals or []),
                )
                for audit in pending
            ]
            for audit_id, resource_type, resource_id, details, risk_signals in snapshots:
                build_number = details.get("skillspector_build_number")
                if build_number is None:
                    _log_collector_event(
                        "item_skipped",
                        audit_id=audit_id,
                        resource_id=resource_id,
                        reason="missing_build_number",
                        next_action="leave_pending",
                    )
                    continue
                try:
                    if await self._collect_one(
                        session,
                        audit_id=audit_id,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        details=details,
                        risk_signals=risk_signals,
                        build_number=build_number,
                    ):
                        processed += 1
                except Exception:
                    # Roll back to keep the session usable for subsequent pending
                    # audits; the snapshot values keep them readable.
                    await session.rollback()
                    collector_logger.exception(
                        "Failed to collect result for audit %s (build #%s)",
                        audit_id, build_number,
                    )
            _log_collector_event(
                "batch_completed",
                pending_count=len(pending),
                completed_count=processed,
            )
            if processed:
                # risk_score 回填会改变可见 skill 数（扫描提交审计时多为
                # pending，count_skills 口径聚合结果是 0），这里补一次对账
                # 把 contributors.skill_count 修正为回填后的实际值
                await self._reconcile_contributor_skill_counts(session)
        return processed

    @staticmethod
    async def _reconcile_contributor_skill_counts(session: Any) -> None:
        try:
            from src.models.repository import ContributorRepository

            corrected = await ContributorRepository(session).reconcile_skill_counts()
            if corrected:
                collector_logger.info(
                    "Contributor skill_count reconciled after audit collection: %d row(s)",
                    corrected,
                )
        except Exception:
            collector_logger.warning(
                "Failed to reconcile contributor skill_count after audit collection",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            try:
                processed = await self.collect_once()
                if processed:
                    collector_logger.info("Collected %d async audit result(s)", processed)
            except Exception:
                collector_logger.exception("SkillspectorCollector loop error")
            await asyncio.sleep(self._poll_interval)

    async def _fetch_pending(self, session: AsyncSession) -> list[Any]:
        # late import to avoid circular deps
        from src.models.orm import SecurityAudit

        result = await session.execute(
            select(SecurityAudit)
            .where(
                SecurityAudit.details["skillspector_async"].as_boolean() == True,  # noqa: E712
                SecurityAudit.details["skillspector_collected"].is_(None),
            )
            .limit(100)
        )
        return list(result.scalars().all())

    async def _collect_one(
        self,
        session: AsyncSession,
        *,
        audit_id: Any,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any],
        risk_signals: list[Any],
        build_number: int,
    ) -> bool:
        """Collect a single Jenkins result.  Returns True if collected.

        Works on plain snapshot values (never the ORM instance) so that a
        session rollback triggered by a previous failure cannot expire the
        attributes this method still needs.
        """
        from src.models.orm import SecurityAudit, Skill, SkillVersion

        collector_logger.debug(
            "Collecting Skillspector result: audit_id=%s build_number=%s",
            audit_id,
            build_number,
        )
        _log_collector_event(
            "item_started",
            audit_id=audit_id,
            resource_id=resource_id,
            resource_type=resource_type,
            build_number=build_number,
        )

        # Probe once instead of waiting here. A long-running build must not
        # block collection of every completed build behind it.
        status = await asyncio.to_thread(self._client.get_build_status, build_number)
        if status in (None, "BUILDING"):
            _log_collector_event(
                "item_deferred",
                audit_id=audit_id,
                resource_id=resource_id,
                build_number=build_number,
                jenkins_status=status or "STATUS_UNAVAILABLE",
                action="retry_next_poll",
                poll_interval_seconds=self._poll_interval,
            )
            return False

        _log_collector_event(
            "status_resolved",
            audit_id=audit_id,
            resource_id=resource_id,
            build_number=build_number,
            jenkins_status=status,
            action="collect_terminal_result",
        )

        collector_logger.info(
            "Jenkins build completed: build_number=%s status=%s",
            build_number,
            status,
        )

        # These terminal states normally cannot produce a complete report.
        if status in {"ABORTED", "NOT_BUILT", "NOT_FOUND"}:
            updated_details = dict(details)
            updated_details["skillspector_collected"] = True
            updated_details["skillspector_status"] = status
            await session.execute(
                update(SecurityAudit)
                .where(SecurityAudit.id == audit_id)
                .values(details=updated_details)
            )
            await session.commit()
            _log_collector_event(
                "item_persisted",
                audit_id=audit_id,
                resource_id=resource_id,
                build_number=build_number,
                outcome="completed_without_report",
                writes={
                    "table": "security_audits",
                    "fields": {
                        "details.skillspector_collected": True,
                        "details.skillspector_status": status,
                    },
                },
                next_action="stop_collecting",
            )
            _log_skillspector_final_result(
                audit_id=audit_id,
                build_number=build_number,
                status=status,
                outcome="completed_without_report",
                error=status.lower(),
            )
            return True

        # Jenkins may report FAILURE when findings make the scanner exit 1,
        # while still archiving a complete report. Fetch for all other terminal
        # statuses, including SUCCESS, FAILURE, and UNSTABLE.
        report, report_status = await asyncio.to_thread(
            self._client.fetch_report_with_status,
            build_number,
        )
        if report is None:
            updated_details, exhausted = _record_unavailable_report_attempt(
                details,
                status,
                terminal=report_status == 404,
            )
            await session.execute(
                update(SecurityAudit)
                .where(SecurityAudit.id == audit_id)
                .values(details=updated_details)
            )
            await session.commit()

            attempts = updated_details["skillspector_report_fetch_attempts"]
            if exhausted:
                collector_logger.error(
                    "Build #%d ended with %s but report is unavailable after %d attempts; "
                    "marking audit as collected",
                    build_number,
                    status,
                    attempts,
                )
                _log_skillspector_final_result(
                    audit_id=audit_id,
                    build_number=build_number,
                    status=status,
                    outcome="completed_without_report",
                    error="report_unavailable",
                )
                _log_collector_event(
                    "item_persisted",
                    audit_id=audit_id,
                    resource_id=resource_id,
                    build_number=build_number,
                    outcome="completed_without_report",
                    report_http_status=report_status,
                    writes={
                        "table": "security_audits",
                        "fields": {
                            "details.skillspector_collected": True,
                            "details.skillspector_status": status,
                            "details.skillspector_error": "report_unavailable",
                            "details.skillspector_report_fetch_attempts": attempts,
                        },
                    },
                    next_action="stop_collecting",
                )
                return True

            _log_collector_event(
                "item_deferred",
                audit_id=audit_id,
                resource_id=resource_id,
                build_number=build_number,
                jenkins_status=status,
                report_http_status=report_status,
                report_fetch_attempts=attempts,
                action="retry_next_poll",
                poll_interval_seconds=self._poll_interval,
            )
            collector_logger.warning(
                "Build #%d ended with %s but report is unavailable; will retry (%d/%d)",
                build_number,
                status,
                attempts,
                MAX_REPORT_FETCH_ATTEMPTS,
            )
            return False

        report = _sanitize_json_value(report)

        # 2. Parse & update
        signals = SkillspectorClient.report_to_risk_signals(report)
        severity = report.get("risk_assessment", {}).get("severity", "LOW").upper()
        score = report.get("risk_assessment", {}).get("score")

        risk_level_map = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high", "CRITICAL": "critical"}
        risk_level = risk_level_map.get(severity, "low")

        details = dict(details)
        details["skillspector_collected"] = True
        details["skillspector_status"] = status
        details["skillspector_score"] = score
        details["skillspector_version"] = report.get("metadata", {}).get("skillspector_version")
        details["recommendation"] = report.get("risk_assessment", {}).get("recommendation")
        details["skillspector_report"] = report
        # 仅当 details 估算大小超过阈值时才截断超长 code_snippet，小报告完整保留
        _maybe_compress_details(details)

        merged_signals = list(risk_signals) + [s.__dict__ for s in signals]

        await session.execute(
            update(SecurityAudit)
            .where(SecurityAudit.id == audit_id)
            .values(risk_level=risk_level, risk_signals=merged_signals, details=details)
        )

        score_write_target = None
        if score is not None:
            skill = await session.get(Skill, resource_id)
            if skill is not None:
                skill.risk_score = score
                score_write_target = "skills.risk_score"
            else:
                skill_version = await session.get(SkillVersion, resource_id)
                if skill_version is not None:
                    skill_version.risk_score = score
                    score_write_target = "skill_versions.risk_score"
                else:
                    collector_logger.warning(
                        "Security audit resource not found: resource_id=%s",
                        resource_id,
                    )

        await session.commit()
        _log_collector_event(
            "item_persisted",
            audit_id=audit_id,
            resource_id=resource_id,
            build_number=build_number,
            outcome="report_collected",
            writes={
                "table": "security_audits",
                "fields": [
                    "risk_level",
                    "risk_signals",
                    "details.skillspector_collected",
                    "details.skillspector_status",
                    "details.skillspector_score",
                    "details.skillspector_version",
                    "details.recommendation",
                    "details.skillspector_report",
                ],
                "risk_level": risk_level,
                "signal_count": len(signals),
                "score_target": score_write_target,
                "score": score,
            },
            next_action="stop_collecting",
        )
        _log_skillspector_final_result(
            audit_id=audit_id,
            build_number=build_number,
            status=status,
            outcome="report_collected",
            risk_level=risk_level,
            score=score,
            signal_count=len(signals),
        )
        collector_logger.info(
            "Collected build #%d → score=%s risk=%s signals=%d",
            build_number, score, risk_level, len(signals),
        )
        return True


# ═══════════════════════════════════════════════════════════════════════════
# SecurityDetector — unified detection engine
# ═══════════════════════════════════════════════════════════════════════════


class SecurityDetector:

    def __init__(self):
        self.enable_audit = settings.security.enable_audit

        # Skillspector (Jenkins-based scanner)
        self._skillspector_client: SkillspectorClient | None = None
        if settings.security.enable_audit:
            self._skillspector_client = SkillspectorClient(
                jenkins_url=settings.security.skillspector_jenkins_url,
                user=settings.security.skillspector_jenkins_user,
                token=settings.security.skillspector_jenkins_token,
                timeout=settings.security.skillspector_timeout,
            )
            if not self._skillspector_client.enabled:
                logger.warning("skillspector enabled but no credentials configured")

    async def detect(self, source: str, source_url: str, metadata: dict[str, Any]) -> SecurityReport:
        return self._create_unknown_report(source_url)

    @staticmethod
    def _calculate_risk_level(risk_signals: list[RiskSignal]) -> str:
        if not risk_signals:
            return "low"

        critical_count = sum(1 for s in risk_signals if s.severity.upper() == "CRITICAL")
        high_count = sum(1 for s in risk_signals if s.severity.upper() == "HIGH")
        medium_count = sum(1 for s in risk_signals if s.severity.upper() == "MEDIUM")

        if critical_count > 0:
            return "critical"
        elif high_count > 0:
            return "high"
        elif medium_count > 0:
            return "medium"
        else:
            return "low"

    @staticmethod
    def _parse_github_url(url: str) -> tuple[str | None, str | None]:
        patterns = [
            r"github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/|$)",
            r"github\.com/([^/]+)/([^/]+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1), match.group(2).replace(".git", "")

        return None, None

    @staticmethod
    def _create_unknown_report(source_url: str) -> SecurityReport:
        return SecurityReport(
            resource_type="skill",
            resource_id=source_url,
            risk_level="unknown",
            risk_signals=[],
            details={"note": "Unknown source type"},
        )

    # ------------------------------------------------------------------
    # Skillspector (Jenkins-based scanner)
    # ------------------------------------------------------------------

    @property
    def has_skillspector(self) -> bool:
        return self._skillspector_client is not None and self._skillspector_client.enabled

    async def trigger_skillspector(
        self,
        source_url: str,
        version: str | None = None,
        skill_path: str = "",
    ) -> int | None:
        """Trigger a Jenkins scan **without waiting** for the result.

        Returns the build number so the caller can poll / collect later.
        """
        if not self.has_skillspector:
            return None
        ref = version if version else "main"
        try:
            return await asyncio.to_thread(
                self._skillspector_client.trigger_scan,
                git_url=source_url,
                ref=ref,
                skill_path=skill_path,
            )
        except Exception as exc:
            logger.error("Skillspector trigger failed: %s", exc)
            return None

    async def detect_skillspector(
        self,
        source_url: str,
        version: str | None = None,
        skill_path: str = "",
    ) -> SecurityReport:
        """Run a Jenkins-triggered Skillspector scan.

        Because the Jenkins client uses synchronous HTTP + polling we offload
        it to a thread to avoid blocking the async event loop.
        """
        if not self.has_skillspector:
            return SecurityReport(
                resource_type="skill",
                resource_id=source_url,
                risk_level="unknown",
                risk_signals=[],
                details={"note": "Skillspector not configured"},
            )

        ref = version if version else "main"

        try:
            report, report_md = await asyncio.to_thread(
                self._skillspector_client.run_scan,
                git_url=source_url,
                ref=ref,
                skill_path=skill_path,
            )
        except Exception as exc:
            logger.error("Skillspector scan failed: %s", exc)
            return SecurityReport(
                resource_type="skill",
                resource_id=source_url,
                risk_level="unknown",
                risk_signals=[],
                details={"error": str(exc), "source": "skillspector"},
            )

        if "error" in report:
            logger.warning("Skillspector returned error: %s", report["error"])
            return SecurityReport(
                resource_type="skill",
                resource_id=source_url,
                risk_level="unknown",
                risk_signals=[],
                details={"error": report["error"], "source": "skillspector"},
            )

        risk_signals = SkillspectorClient.report_to_risk_signals(report)
        severity = report.get("risk_assessment", {}).get("severity", "UNKNOWN").upper()
        score = report.get("risk_assessment", {}).get("score")

        risk_level_map = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high", "CRITICAL": "critical"}
        risk_level = risk_level_map.get(severity, "unknown")

        details: dict[str, Any] = {
            "source": "skillspector",
            "skillspector_version": report.get("metadata", {}).get("skillspector_version"),
            "skillspector_score": score,
            "recommendation": report.get("risk_assessment", {}).get("recommendation"),
            "skillspector_report": report,
        }
        if report_md:
            details["skillspector_report_md"] = report_md
        # 仅当 details 估算大小超过阈值时才截断超长 code_snippet，小报告完整保留
        _maybe_compress_details(details)

        return SecurityReport(
            resource_type="skill",
            resource_id=source_url,
            risk_level=risk_level,
            risk_signals=risk_signals,
            details=details,
        )

    async def fetch_external_report_md(self, build_number: int) -> str | None:
        """Fetch the Markdown report (report.md) for a one-off scan build.

        Used by the ``audit-by-url/report`` endpoint so PR reviewers can
        download the full report for a single skill scan.
        """
        if not self.has_skillspector:
            return None
        try:
            return await asyncio.to_thread(
                self._skillspector_client.fetch_report_md, build_number
            )
        except Exception as exc:
            logger.error("Skillspector report.md fetch failed: %s", exc)
            return None

    async def get_external_result(
        self, build_number: int
    ) -> dict[str, Any]:
        """Poll a previously-triggered one-off Skillspector scan (async mode).

        Used by the ``audit-by-url`` result endpoint: the PR gate triggers a
        scan with ``async_mode=True`` and polls this method until the Jenkins
        build finishes, then collects report.json / report.md without
        persisting anything to the database.

        Returns a dict with a ``status`` field:
        * ``"pending"``   - build still running or status unavailable
        * ``"done"``      - report collected, includes risk_level/score/signals
        * ``"error"``     - build not found or report could not be fetched
        """
        if not self.has_skillspector:
            return {
                "status": "error",
                "build_number": build_number,
                "error": "Skillspector not configured",
            }

        try:
            status = await asyncio.to_thread(
                self._skillspector_client.get_build_status, build_number
            )
        except Exception as exc:
            logger.error("Skillspector status fetch failed: %s", exc)
            return {
                "status": "error",
                "build_number": build_number,
                "error": str(exc)[:200],
            }

        if status in (None, "BUILDING"):
            return {
                "status": "pending",
                "build_number": build_number,
                "jenkins_status": status or "BUILDING",
            }
        if status == "NOT_FOUND":
            return {
                "status": "error",
                "build_number": build_number,
                "jenkins_status": status,
                "error": f"Build {build_number} not found",
            }

        try:
            report = await asyncio.to_thread(
                self._skillspector_client.fetch_report, build_number
            )
            report_md = await asyncio.to_thread(
                self._skillspector_client.fetch_report_md, build_number
            )
        except Exception as exc:
            logger.error("Skillspector report fetch failed: %s", exc)
            return {
                "status": "error",
                "build_number": build_number,
                "jenkins_status": status,
                "error": str(exc)[:200],
            }

        if report is None:
            return {
                "status": "error",
                "build_number": build_number,
                "jenkins_status": status,
                "error": f"Failed to fetch report for build {build_number}",
            }

        risk_signals = SkillspectorClient.report_to_risk_signals(report)
        severity = report.get("risk_assessment", {}).get("severity", "UNKNOWN").upper()
        score = report.get("risk_assessment", {}).get("score")
        risk_level_map = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high", "CRITICAL": "critical"}
        risk_level = risk_level_map.get(severity, "unknown")

        details: dict[str, Any] = {
            "source": "skillspector",
            "skillspector_build_number": build_number,
            "skillspector_version": report.get("metadata", {}).get("skillspector_version"),
            "skillspector_score": score,
            "recommendation": report.get("risk_assessment", {}).get("recommendation"),
            "skillspector_report": report,
        }
        if report_md:
            details["skillspector_report_md"] = report_md

        return {
            "status": "done",
            "build_number": build_number,
            "jenkins_status": status,
            "risk_level": risk_level,
            "risk_score": score,
            "risk_signals": [
                {
                    "id": s.id,
                    "name": s.name,
                    "description": s.description,
                    "severity": s.severity,
                    "data": s.data,
                }
                for s in risk_signals
            ],
            "details": details,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Factory — start the background collector from app lifecycle
# ═══════════════════════════════════════════════════════════════════════════


async def start_skillspector_collector() -> SkillspectorCollector | None:
    """Create and start the Skillspector background collector.

    Returns the collector instance for later shutdown, or *None* if credentials
    are not configured.
    """
    from src.core.database import AsyncSessionLocal

    client = SkillspectorClient(
        jenkins_url=settings.security.skillspector_jenkins_url,
        user=settings.security.skillspector_jenkins_user,
        token=settings.security.skillspector_jenkins_token,
        timeout=settings.security.skillspector_timeout,
    )
    if not client.enabled:
        collector_logger.warning("Skillspector enabled but no credentials — collector skipped")
        return None

    collector = SkillspectorCollector(
        client=client,
        session_factory=AsyncSessionLocal,
        # 2 分钟一次：Jenkins 构建本身需要数分钟，轮询过密只是空转打日志
        # （_log_collector_event 每次轮询都有条目），120s 既不延迟结果回写太多，
        # 又把轮询日志频率降到原来的 1/4
        poll_interval=120,
    )
    await collector.start()
    collector_logger.info("SkillspectorCollector background task started")
    return collector
