"""Unit tests for API health probes and operational metrics endpoints."""

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import get_db
from app.db.models import Base
from app.main import app


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    """Provide an isolated in-memory SQLite database session."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """TestClient with overridden database dependency."""
    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_top_level_root(client: TestClient) -> None:
    """Root endpoint returns online status."""
    res = client.get("/")
    assert res.status_code == 200
    assert res.json() == {"status": "online", "system": "HealthKicks Cloud API"}


def test_top_level_health(client: TestClient) -> None:
    """Top-level /health probe returns ok and uptime."""
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "uptime_seconds" in data
    assert isinstance(data["uptime_seconds"], (int, float))


def test_top_level_metrics(client: TestClient) -> None:
    """Top-level /metrics returns operational summary."""
    res = client.get("/metrics")
    assert res.status_code == 200
    data = res.json()
    assert "uptime_seconds" in data
    assert "total_requests" in data
    assert "status_distribution" in data
    assert "2xx" in data["status_distribution"]


def test_api_v1_health(client: TestClient) -> None:
    """API v1 /api/v1/health returns deep health probe including database and latency."""
    res = client.get("/api/v1/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["database"] == "ok"
    assert "uptime_seconds" in data
    assert "database_latency_ms" in data
    assert isinstance(data["database_latency_ms"], (int, float))


def test_api_v1_health_degraded_when_database_fails() -> None:
    """API v1 /api/v1/health returns degraded status if database connection fails."""
    class BrokenSession:
        def execute(self, *args, **kwargs):
            raise RuntimeError("Database connection down")

    def broken_get_db():
        yield BrokenSession()

    app.dependency_overrides[get_db] = broken_get_db
    try:
        with TestClient(app) as broken_client:
            res = broken_client.get("/api/v1/health")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "degraded"
            assert data["database"] == "unavailable"
    finally:
        app.dependency_overrides.clear()


def test_api_v1_health_liveness(client: TestClient) -> None:
    """API v1 /api/v1/health/liveness returns 200 fast status."""
    res = client.get("/api/v1/health/liveness")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_api_v1_health_readiness(client: TestClient) -> None:
    """API v1 /api/v1/health/readiness verifies database connectivity."""
    res = client.get("/api/v1/health/readiness")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["database"] == "ok"


def test_api_v1_metrics(client: TestClient) -> None:
    """API v1 /api/v1/metrics returns metrics breakdown including top endpoints."""
    res = client.get("/api/v1/metrics")
    assert res.status_code == 200
    data = res.json()
    assert data["total_requests"] > 0
    assert "average_latency_ms" in data
    assert "top_endpoints" in data


def test_top_level_metrics_prometheus_format(client: TestClient) -> None:
    """Top-level /metrics returns standard Prometheus exposition text when requested."""
    res = client.get("/metrics", headers={"Accept": "text/plain"})
    assert res.status_code == 200
    assert "text/plain" in res.headers["content-type"]
    text_content = res.text
    assert "# HELP healthkicks_uptime_seconds" in text_content
    assert "# TYPE healthkicks_uptime_seconds gauge" in text_content
    assert "healthkicks_uptime_seconds" in text_content
    assert "# HELP healthkicks_http_requests_total" in text_content
    assert 'healthkicks_http_requests_total{status_class="2xx"}' in text_content
    assert "# HELP healthkicks_http_request_duration_seconds" in text_content
    assert "# TYPE healthkicks_http_request_duration_seconds histogram" in text_content
    assert 'healthkicks_http_request_duration_seconds_bucket{le="1.0"}' in text_content
    assert 'healthkicks_http_request_duration_seconds_bucket{le="+Inf"}' in text_content
    assert "healthkicks_http_request_duration_seconds_sum" in text_content
    assert "healthkicks_http_request_duration_seconds_count" in text_content


def test_api_v1_metrics_prometheus_query_param(client: TestClient) -> None:
    """API v1 /api/v1/metrics supports ?format=prometheus query parameter."""
    res = client.get("/api/v1/metrics?format=prometheus")
    assert res.status_code == 200
    assert "text/plain" in res.headers["content-type"]
    text_content = res.text
    assert "healthkicks_uptime_seconds" in text_content
    assert "healthkicks_http_endpoint_requests_total" in text_content
