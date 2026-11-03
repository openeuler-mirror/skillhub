import pytest
from fastapi.testclient import TestClient

from src.api.main import app


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


def test_list_skills_empty(client):
    response = client.get("/api/v1/skills/")
    assert response.status_code == 200
    data = response.json()
    assert data["code"] == 200
    assert data["msg"] == "ok"
    assert "skills" in data["data"]
    assert "total" in data["data"]


def test_get_skill_not_found(client):
    response = client.get("/api/v1/skills/nonexistent")
    assert response.status_code == 404


def test_search_endpoint(client):
    response = client.get("/api/v1/index/search", params={"q": "test"})
    assert response.status_code == 200
    data = response.json()
    assert data["code"] == 200
    assert data["msg"] == "ok"
    assert "results" in data["data"]
    assert "total" in data["data"]


def test_categories_endpoint(client):
    response = client.get("/api/v1/index/categories")
    assert response.status_code == 200
    data = response.json()
    assert data["code"] == 200
    assert "categories" in data["data"]


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