"""Remote raw-file adapters for the git hosts used by WittyHub.

Only hosts that WittyHub actually stores are supported: GitHub, GitCode,
GitLab and Gitee.  Raw URLs are built from a validated :class:`RepoRef`
instead of being taken from the database, so the request host is always one
of a fixed allow-list.  Credentials, explicit ports, query strings and
redirects are rejected; a file that cannot be fetched safely is reported as
:class:`RawFetchError` and the caller falls back to the stored snippet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

import httpx

__all__ = [
    "SUPPORTED_HOSTS",
    "RawFetchError",
    "RemoteTokens",
    "RepoRef",
    "build_raw_url",
    "fetch_raw_file",
    "is_binary",
    "normalise_repo_path",
    "parse_repo_url",
]

SUPPORTED_HOSTS = frozenset({"github.com", "gitcode.com", "gitlab.com", "gitee.com"})

_SSH_RE = re.compile(r"^git@([^:]+):(.+)$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_BINARY_CONTROL_RATIO = 0.10


class RawFetchError(RuntimeError):
    """A raw file could not be fetched; ``reason`` is stable for metrics."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class RepoRef:
    """A parsed repository reference: ``host/namespace/repo``."""

    host: str
    namespace: str
    repo: str

    @property
    def full_path(self) -> str:
        return f"{self.namespace}/{self.repo}"


@dataclass(frozen=True)
class RemoteTokens:
    """Optional per-host access tokens; empty means public repositories only."""

    github: str = ""
    gitcode: str = ""
    gitlab: str = ""
    gitee: str = ""


def parse_repo_url(repo_url: str | None) -> RepoRef | None:
    """Parse a supported git repository URL into ``host/namespace/repo``.

    Returns ``None`` for unsupported hosts or malformed URLs.  Blob/tree URLs
    are rejected because they are not repository roots, and credentials,
    explicit ports, query strings and fragments are rejected to keep the
    fetcher's SSRF surface as small as possible.
    """
    if not repo_url:
        return None
    text = repo_url.strip()
    ssh_match = _SSH_RE.match(text)
    if ssh_match:
        host = ssh_match.group(1).strip().lower()
        raw_path = ssh_match.group(2).strip()
    elif "://" in text:
        try:
            parsed = urlsplit(text)
            if parsed.scheme not in {"http", "https"}:
                return None
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                return None
            port = parsed.port
            host = (parsed.hostname or "").lower()
        except ValueError:
            # 非法端口（https://host:bad/...）或非法 IPv6 字面量
            return None
        if port not in (None, 80, 443):
            return None
        raw_path = parsed.path.strip("/")
    else:
        return None

    if host not in SUPPORTED_HOSTS:
        return None
    if any(marker in raw_path for marker in ("/blob/", "/tree/", "/-/")):
        return None

    segments = [unquote(segment) for segment in raw_path.split("/") if segment]
    if len(segments) < 2:
        return None
    if any(segment in {".", ".."} or not _SEGMENT_RE.fullmatch(segment) for segment in segments):
        return None
    if segments[-1].endswith(".git"):
        segments[-1] = segments[-1][:-4]
        if not segments[-1]:
            return None
    return RepoRef(host=host, namespace="/".join(segments[:-1]), repo=segments[-1])


def normalise_repo_path(path: str | None) -> str:
    """Normalise a repo-relative path; reject absolute paths and traversal."""
    text = (path or "").replace("\\", "/").strip()
    if not text or text.startswith("/") or "\x00" in text:
        raise RawFetchError("invalid_path", path or "")
    segments = [segment for segment in text.split("/") if segment not in {"", "."}]
    if not segments or any(segment == ".." for segment in segments):
        raise RawFetchError("invalid_path", path or "")
    return "/".join(segments)


def build_raw_url(ref: RepoRef, commit: str, path: str) -> str:
    """Build the platform-specific raw URL for ``commit:path``."""
    if not _COMMIT_RE.fullmatch(commit or ""):
        raise RawFetchError("invalid_commit", commit or "")
    normalised = normalise_repo_path(path)
    safe_path = quote(normalised, safe="/")

    if ref.host == "github.com":
        return f"https://raw.githubusercontent.com/{ref.full_path}/{commit}/{safe_path}"
    if ref.host == "gitcode.com":
        return f"https://raw.gitcode.com/{ref.full_path}/raw/{commit}/{safe_path}"
    if ref.host == "gitlab.com":
        project = quote(ref.full_path, safe="")
        file_path = quote(normalised, safe="")
        return (
            f"https://gitlab.com/api/v4/projects/{project}"
            f"/repository/files/{file_path}/raw?ref={quote(commit, safe='')}"
        )
    if ref.host == "gitee.com":
        # gitee.com/.../raw/... 302-redirects to this independent host; using
        # the final host directly lets the fetcher keep redirects disabled.
        return f"https://raw.giteeusercontent.com/{ref.full_path}/raw/{commit}/{safe_path}"
    raise RawFetchError("unsupported_host", ref.host)


def _auth_for(ref: RepoRef, tokens: RemoteTokens) -> tuple[dict[str, str], dict[str, str]]:
    headers: dict[str, str] = {}
    params: dict[str, str] = {}
    if ref.host == "github.com" and tokens.github:
        headers["Authorization"] = f"Bearer {tokens.github}"
    elif ref.host == "gitcode.com" and tokens.gitcode:
        # GitCode's raw endpoint accepts the access token as a query parameter.
        params["access_token"] = tokens.gitcode
    elif ref.host == "gitlab.com" and tokens.gitlab:
        headers["PRIVATE-TOKEN"] = tokens.gitlab
    elif ref.host == "gitee.com" and tokens.gitee:
        headers["Authorization"] = f"Bearer {tokens.gitee}"
    return headers, params


def is_binary(data: bytes) -> bool:
    """Heuristic binary check shared by the remote and local readers."""
    sample = data[:8192]
    if b"\x00" in sample:
        return True
    control = sum(1 for byte in sample if byte < 9 or 13 < byte < 32 or byte == 127)
    return bool(sample) and control / len(sample) > _BINARY_CONTROL_RATIO


async def fetch_raw_file(
    client: httpx.AsyncClient,
    ref: RepoRef,
    commit: str,
    path: str,
    *,
    tokens: RemoteTokens | None = None,
    timeout: float = 15.0,
    max_bytes: int = 512 * 1024,
) -> str:
    """Fetch one raw text file, enforcing timeout, size and binary limits.

    Redirects are intentionally not followed: every supported platform has a
    direct raw endpoint above, and a redirect could point outside the allow-list.
    """
    url = build_raw_url(ref, commit, path)
    headers, params = _auth_for(ref, tokens or RemoteTokens())
    try:
        async with client.stream(
            "GET",
            url,
            headers=headers,
            params=params,
            timeout=timeout,
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise RawFetchError(f"http_{response.status_code}", f"{ref.full_path}:{path}")
            declared_length = response.headers.get("content-length")
            if declared_length:
                try:
                    if int(declared_length) > max_bytes:
                        raise RawFetchError("too_large", declared_length)
                except ValueError:
                    pass

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise RawFetchError("too_large", str(total))
                chunks.append(chunk)
    except httpx.TimeoutException as exc:
        raise RawFetchError("timeout", f"{ref.full_path}:{path}") from exc
    except httpx.TransportError as exc:
        raise RawFetchError("network", f"{ref.full_path}:{path}") from exc

    data = b"".join(chunks)
    if not data:
        raise RawFetchError("empty", f"{ref.full_path}:{path}")
    if is_binary(data):
        raise RawFetchError("binary", f"{ref.full_path}:{path}")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RawFetchError("binary", f"{ref.full_path}:{path}") from exc
