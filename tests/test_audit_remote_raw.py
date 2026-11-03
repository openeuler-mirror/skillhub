import httpx
import pytest

from src.security.audit_review.remote_raw import (
    RawFetchError,
    RemoteTokens,
    RepoRef,
    build_raw_url,
    fetch_raw_file,
    normalise_repo_path,
    parse_repo_url,
)

SHA = "a" * 40


@pytest.mark.parametrize(
    ("url", "host", "namespace", "repo"),
    [
        ("https://github.com/owner/repo", "github.com", "owner", "repo"),
        ("https://github.com/owner/repo.git/", "github.com", "owner", "repo"),
        ("git@github.com:owner/repo.git", "github.com", "owner", "repo"),
        ("https://gitcode.com/owner/repo", "gitcode.com", "owner", "repo"),
        ("https://gitlab.com/group/sub/repo.git", "gitlab.com", "group/sub", "repo"),
        ("git@gitlab.com:group/sub/repo.git", "gitlab.com", "group/sub", "repo"),
        ("https://gitee.com/owner/repo", "gitee.com", "owner", "repo"),
    ],
)
def test_parse_repo_url(url, host, namespace, repo):
    parsed = parse_repo_url(url)
    assert parsed == RepoRef(host=host, namespace=namespace, repo=repo)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "https://bitbucket.org/owner/repo",
        "https://user:pass@github.com/owner/repo",
        "https://github.com:8443/owner/repo",
        "https://github.com/owner/repo?tab=readme",
        "https://github.com/owner/repo/blob/main/SKILL.md",
        "https://gitlab.com/group/-/tree/main",
        "https://github.com/owner",
        "https://github.com/../repo",
        "ssh://git@github.com/owner/repo.git",
        "https://github.com:bad/owner/repo",
        "https://[::1/owner/repo",
    ],
)
def test_parse_repo_url_rejects_malformed_or_unsupported(url):
    assert parse_repo_url(url) is None


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        (
            "github.com",
            f"https://raw.githubusercontent.com/owner/repo/{SHA}/dir/a%20b.md",
        ),
        (
            "gitcode.com",
            f"https://raw.gitcode.com/owner/repo/raw/{SHA}/dir/a%20b.md",
        ),
        (
            "gitlab.com",
            f"https://gitlab.com/api/v4/projects/owner%2Frepo"
            f"/repository/files/dir%2Fa%20b.md/raw?ref={SHA}",
        ),
        (
            "gitee.com",
            f"https://raw.giteeusercontent.com/owner/repo/raw/{SHA}/dir/a%20b.md",
        ),
    ],
)
def test_build_raw_url(host, expected):
    ref = RepoRef(host=host, namespace="owner", repo="repo")
    assert build_raw_url(ref, SHA, "dir/a b.md") == expected


def test_build_raw_url_gitlab_nested_namespace():
    ref = RepoRef(host="gitlab.com", namespace="group/sub", repo="repo")
    assert (
        build_raw_url(ref, SHA, "README.md")
        == f"https://gitlab.com/api/v4/projects/group%2Fsub%2Frepo"
        f"/repository/files/README.md/raw?ref={SHA}"
    )


@pytest.mark.parametrize(
    "path",
    ["", "/etc/passwd", "../secret", "dir/../secret", "dir\\..\\secret", "a\x00b"],
)
def test_normalise_repo_path_rejects_traversal(path):
    with pytest.raises(RawFetchError) as error:
        normalise_repo_path(path)
    assert error.value.reason == "invalid_path"


def test_normalise_repo_path_keeps_nested_path():
    assert normalise_repo_path("./dir//a.md") == "dir/a.md"


async def test_fetch_raw_file_success():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (f"https://raw.githubusercontent.com/owner/repo/{SHA}/dir/a.md")
        return httpx.Response(200, content=b"# hello")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        text = await fetch_raw_file(client, RepoRef("github.com", "owner", "repo"), SHA, "dir/a.md")
    assert text == "# hello"


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(404), "http_404"),
        (httpx.Response(403), "http_403"),
        (httpx.Response(429), "http_429"),
        (httpx.Response(500), "http_500"),
        (httpx.Response(302, headers={"location": "https://evil.example/"}), "http_302"),
        (httpx.Response(200, content=b"\x00abc"), "binary"),
        (httpx.Response(200, content=b""), "empty"),
    ],
)
async def test_fetch_raw_file_rejections(response, reason):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RawFetchError) as error:
            await fetch_raw_file(client, RepoRef("github.com", "owner", "repo"), SHA, "a.md")
    assert error.value.reason == reason


async def test_fetch_raw_file_too_large_by_content_length():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x", headers={"content-length": "999999"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RawFetchError) as error:
            await fetch_raw_file(client, RepoRef("github.com", "owner", "repo"), SHA, "a.md")
    assert error.value.reason == "too_large"


class _ChunkStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"a" * 30
        yield b"b" * 30


async def test_fetch_raw_file_too_large_while_streaming():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_ChunkStream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RawFetchError) as error:
            await fetch_raw_file(
                client,
                RepoRef("github.com", "owner", "repo"),
                SHA,
                "a.md",
                max_bytes=50,
            )
    assert error.value.reason == "too_large"


async def test_fetch_raw_file_timeout():
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RawFetchError) as error:
            await fetch_raw_file(client, RepoRef("github.com", "owner", "repo"), SHA, "a.md")
    assert error.value.reason == "timeout"


@pytest.mark.parametrize(
    ("ref", "token_name", "check"),
    [
        (
            RepoRef("github.com", "o", "r"),
            "github",
            lambda r: r.headers["authorization"] == "Bearer gh",
        ),
        (RepoRef("gitlab.com", "o", "r"), "gitlab", lambda r: r.headers["private-token"] == "gl"),
        (
            RepoRef("gitee.com", "o", "r"),
            "gitee",
            lambda r: r.headers["authorization"] == "Bearer ge",
        ),
        (
            RepoRef("gitcode.com", "o", "r"),
            "gitcode",
            lambda r: r.url.params["access_token"] == "gc",
        ),
    ],
)
async def test_fetch_raw_file_sends_tokens(ref, token_name, check):
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ok")

    tokens = RemoteTokens(github="gh", gitcode="gc", gitlab="gl", gitee="ge")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        text = await fetch_raw_file(client, ref, SHA, "a.md", tokens=tokens)

    assert text == "ok"
    assert check(seen[0])
