"""Tests for the Cloud API contracts and ingestion endpoint."""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.api.v1.ingestion import settings as ingestion_settings
from app.db.database import get_db
from app.db.models import Base, FallEvent
from app.main import app
from app.schemas.ingestion import IngestionEvent


def test_openapi_exposes_cloud_and_ingestion_endpoints() -> None:
    paths = app.openapi()["paths"]
    assert "/api/v1/ingest/event" in paths
    assert "/api/v1/devices/{device_id}/haptic/trigger" in paths
    assert "/api/telemetry/latest" not in paths


def test_header_accepts_timestamp_aliases() -> None:
    payload = {"event_type": "fall", "confidence_score": 0.95, "raw_imu_snapshot": {"ax": 1.0}}
    header = {"device_id": "shoe-1", "msg_id": "message-1", "timestamp": "2026-01-01T00:00:00Z"}
    assert IngestionEvent.model_validate({"header": header, "payload": payload}).header.timestamp_utc.year == 2026
    header["timestamp_utc"] = header.pop("timestamp")
    assert IngestionEvent.model_validate({"header": header, "payload": payload}).header.timestamp_utc.year == 2026


def test_ingestion_route_auth_and_idempotence(monkeypatch) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    def override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr("app.api.v1.ingestion.settings", ingestion_settings.__class__(ingest_token="secret", environment="production"))
    payload = {
        "header": {"device_id": "shoe-1", "msg_id": "message-1", "timestamp": "2026-01-01T00:00:00Z"},
        "payload": {"event_type": "fall", "confidence_score": 0.95, "raw_imu_snapshot": {"ax": 1.0}},
    }
    try:
        with TestClient(app) as client:
            assert client.post("/api/v1/ingest/event", json=payload).status_code == 401
            headers = {"X-HealthKicks-Ingest-Token": "secret"}
            assert client.post("/api/v1/ingest/event", json=payload, headers=headers).json()["duplicate"] is False
            assert client.post("/api/v1/ingest/event", json=payload, headers=headers).json()["duplicate"] is True
        with session_factory() as session:
            assert session.query(FallEvent).count() == 1
    finally:
        app.dependency_overrides.clear()
