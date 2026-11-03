import pytest

from src.security.detector import validate_git_url


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/openai/codex.git",
        "ssh://git@gitlab.com/group/project.git",
        "https://atomgit.com/openeuler/wittyhub",
        "https://atomgit.com/openeuler/wittyhub",
    ],
)
def test_validate_git_url_accepts_allowlisted_repository_urls(url):
    assert validate_git_url(url) == (True, "")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/repository.git",
        "http://169.254.169.254/latest/meta-data",
        "https://github.com.evil.example/repository.git",
        "https://attacker.github.com/repository.git",
        "https://user:password@github.com/repository.git",
        "https://github.com:8443/repository.git",
        "https://github.com/owner/../repository.git",
        "https://github.com/owner/repository.git#fragment",
        "file:///etc/passwd",
    ],
)
def test_validate_git_url_rejects_ssrf_and_ambiguous_urls(url):
    is_valid, error = validate_git_url(url)

    assert is_valid is False
    assert error
