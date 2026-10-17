"""Unit tests for API health probes and operational metrics endpoints."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_top_level_root() -> None:
    """Root endpoint returns online status."""
    res = client.get("/")
    assert res.status_code == 200
    assert res.json() == {"status": "online", "system": "HealthKicks Cloud API"}


def test_top_level_health() -> None:
    """Top-level /health probe returns ok and uptime."""
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "uptime_seconds" in data
    assert isinstance(data["uptime_seconds"], (int, float))


def test_top_level_metrics() -> None:
    """Top-level /metrics returns operational summary."""
    res = client.get("/metrics")
    assert res.status_code == 200
    data = res.json()
    assert "uptime_seconds" in data
    assert "total_requests" in data
    assert "status_distribution" in data
    assert "2xx" in data["status_distribution"]


def test_api_v1_health() -> None:
    """API v1 /api/v1/health returns deep health probe including database and latency."""
    res = client.get("/api/v1/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["database"] == "ok"
    assert "uptime_seconds" in data
    assert "database_latency_ms" in data
    assert isinstance(data["database_latency_ms"], (int, float))


def test_api_v1_health_liveness() -> None:
    """API v1 /api/v1/health/liveness returns 200 fast status."""
    res = client.get("/api/v1/health/liveness")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_api_v1_health_readiness() -> None:
    """API v1 /api/v1/health/readiness verifies database connectivity."""
    res = client.get("/api/v1/health/readiness")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["database"] == "ok"


def test_api_v1_metrics() -> None:
    """API v1 /api/v1/metrics returns metrics breakdown including top endpoints."""
    res = client.get("/api/v1/metrics")
    assert res.status_code == 200
    data = res.json()
    assert data["total_requests"] > 0
    assert "average_latency_ms" in data
    assert "top_endpoints" in data

