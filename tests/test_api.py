import socket

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.core.config import get_settings


def _postgres_available() -> bool:
    """探测配置的 PostgreSQL 是否可连接，用于跳过依赖真实数据库的用例。"""
    settings = get_settings()
    try:
        with socket.create_connection(
            (settings.postgres.host, settings.postgres.port), timeout=1
        ):
            return True
    except OSError:
        return False


requires_db = pytest.mark.skipif(
    not _postgres_available(),
    reason="requires a running PostgreSQL (deploy/compose.yaml)",
)


@pytest.fixture
def client():
    return TestClient(app)


def test_health_check(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["msg"] == "ok"
    assert body["data"] == {"status": "healthy", "service": "skillhub"}


@requires_db
def test_list_skills_empty(client):
    response = client.get("/api/v1/skills/")
    assert response.status_code == 200
    data = response.json()
    assert data["code"] == 200
    assert data["msg"] == "ok"
    assert "skills" in data["data"]
    assert "total" in data["data"]


@requires_db
def test_get_skill_not_found(client):
    response = client.get("/api/v1/skills/nonexistent")
    assert response.status_code == 404


@requires_db
def test_search_endpoint(client):
    response = client.get("/api/v1/index/search", params={"q": "test"})
    assert response.status_code == 200
    data = response.json()
    assert data["code"] == 200
    assert data["msg"] == "ok"
    assert "results" in data["data"]
    assert "total" in data["data"]


@requires_db
def test_categories_endpoint(client):
    response = client.get("/api/v1/index/categories")
    assert response.status_code == 200
    data = response.json()
    assert data["code"] == 200
    assert "categories" in data["data"]


@requires_db
def test_stats_endpoint(client):
    response = client.get("/api/v1/index/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["code"] == 200
    assert "total_skills" in data["data"]
    assert "total_categories" in data["data"]
    assert "categories" in data["data"]
    # Each category exposes a Chinese display label alongside the English key
    for cat in data["data"]["categories"]:
        assert "name" in cat
        assert "label" in cat
        assert cat["label"]