"""Tests for /api/v1/internal/device-presence endpoint."""

import dataclasses
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import config as config_module
from app.db.database import get_db
from app.db.models import Base, Device, DeviceStatus
from app.main import app

INGEST_TOKEN = "secret-test-ingest-token"
TEST_DEVICE_ID = "HK-2"


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def client(db_session, monkeypatch):
    patched_settings = dataclasses.replace(config_module.settings, ingest_token=INGEST_TOKEN)
    monkeypatch.setattr("app.api.v1.internal.settings", patched_settings)

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def seeded_device(db_session) -> Device:
    device = Device(
        device_id=TEST_DEVICE_ID,
        name="HealthKicks Shoe 2",
        status=DeviceStatus.offline,
        last_seen_utc=None,
    )
    db_session.add(device)
    db_session.commit()
    db_session.refresh(device)
    return device


def test_openapi_exposes_internal_device_presence() -> None:
    paths = app.openapi()["paths"]
    assert "/api/v1/internal/device-presence" in paths


def test_missing_ingest_token_rejected(client) -> None:
    payload = {
        "device_id": TEST_DEVICE_ID,
        "status": "online",
        "timestamp": "2026-09-03T18:00:00Z",
    }
    response = client.post("/api/v1/internal/device-presence", json=payload)
    assert response.status_code == 401
    assert "Invalid or missing ingestion token" in response.json()["detail"]


def test_invalid_ingest_token_rejected(client) -> None:
    payload = {
        "device_id": TEST_DEVICE_ID,
        "status": "online",
        "timestamp": "2026-09-03T18:00:00Z",
    }
    headers = {"X-Ingest-Token": "wrong-token"}
    response = client.post("/api/v1/internal/device-presence", json=payload, headers=headers)
    assert response.status_code == 401
    assert "Invalid or missing ingestion token" in response.json()["detail"]


def test_unknown_device_raises_404(client) -> None:
    payload = {
        "device_id": "HK-999",
        "status": "online",
        "timestamp": "2026-09-03T18:00:00Z",
    }
    headers = {"X-Ingest-Token": INGEST_TOKEN}
    response = client.post("/api/v1/internal/device-presence", json=payload, headers=headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "Device not found"


def test_online_transition_updates_status_and_last_seen(client, db_session, seeded_device) -> None:
    online_time_str = "2026-09-03T18:00:00Z"
    payload = {
        "device_id": TEST_DEVICE_ID,
        "status": "online",
        "timestamp": online_time_str,
    }
    headers = {"X-Ingest-Token": INGEST_TOKEN}
    response = client.post("/api/v1/internal/device-presence", json=payload, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data == {
        "status": "ok",
        "device_id": TEST_DEVICE_ID,
        "device_status": "online",
    }

    # Verify DB state
    db_session.refresh(seeded_device)
    assert seeded_device.status == DeviceStatus.online
    assert seeded_device.last_seen_utc is not None
    assert seeded_device.last_seen_utc.year == 2026
    assert seeded_device.last_seen_utc.hour == 18


def test_connected_alias_updates_status_and_last_seen(client, db_session, seeded_device) -> None:
    connected_time_str = "2026-09-03T18:15:00Z"
    payload = {
        "device_id": TEST_DEVICE_ID,
        "status": "connected",
        "timestamp": connected_time_str,
    }
    headers = {"X-Ingest-Token": INGEST_TOKEN}
    response = client.post("/api/v1/internal/device-presence", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["device_status"] == "online"

    db_session.refresh(seeded_device)
    assert seeded_device.status == DeviceStatus.online
    assert seeded_device.last_seen_utc is not None
    assert seeded_device.last_seen_utc.minute == 15


def test_offline_transition_updates_status_without_overwriting_last_seen(
    client, db_session, seeded_device
) -> None:
    # 1. First bring online to establish last_seen_utc
    online_timestamp = datetime(2026, 9, 3, 18, 0, 0, tzinfo=timezone.utc)
    seeded_device.status = DeviceStatus.online
    seeded_device.last_seen_utc = online_timestamp
    db_session.commit()

    # 2. Trigger offline transition with a later disconnect timestamp
    offline_payload = {
        "device_id": TEST_DEVICE_ID,
        "status": "offline",
        "timestamp": "2026-09-03T18:30:00Z",
    }
    headers = {"X-Ingest-Token": INGEST_TOKEN}
    response = client.post("/api/v1/internal/device-presence", json=offline_payload, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data == {
        "status": "ok",
        "device_id": TEST_DEVICE_ID,
        "device_status": "offline",
    }

    # Verify DB state: status is offline, but last_seen_utc was preserved
    db_session.refresh(seeded_device)
    assert seeded_device.status == DeviceStatus.offline
    # In SQLite, timezone may be naive or aware, check timestamp components
    assert seeded_device.last_seen_utc.hour == 18
    assert seeded_device.last_seen_utc.minute == 0  # Preserved from online timestamp, NOT 30!


def test_disconnected_alias_updates_status_without_overwriting_last_seen(
    client, db_session, seeded_device
) -> None:
    online_timestamp = datetime(2026, 9, 3, 18, 5, 0, tzinfo=timezone.utc)
    seeded_device.status = DeviceStatus.online
    seeded_device.last_seen_utc = online_timestamp
    db_session.commit()

    payload = {
        "device_id": TEST_DEVICE_ID,
        "status": "disconnected",
        "timestamp": "2026-09-03T18:45:00Z",
    }
    headers = {"X-Ingest-Token": INGEST_TOKEN}
    response = client.post("/api/v1/internal/device-presence", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["device_status"] == "offline"

    db_session.refresh(seeded_device)
    assert seeded_device.status == DeviceStatus.offline
    assert seeded_device.last_seen_utc.minute == 5  # Preserved, NOT 45!


def test_invalid_status_value_returns_400(client, seeded_device) -> None:
    payload = {
        "device_id": TEST_DEVICE_ID,
        "status": "sleeping",
        "timestamp": "2026-09-03T18:00:00Z",
    }
    headers = {"X-Ingest-Token": INGEST_TOKEN}
    response = client.post("/api/v1/internal/device-presence", json=payload, headers=headers)
    assert response.status_code == 400
    assert "Invalid status" in response.json()["detail"]


def test_support_for_x_healthkicks_ingest_token_alias(client, seeded_device) -> None:
    payload = {
        "device_id": TEST_DEVICE_ID,
        "status": "online",
        "timestamp": "2026-09-03T18:00:00Z",
    }
    headers = {"X-HealthKicks-Ingest-Token": INGEST_TOKEN}
    response = client.post("/api/v1/internal/device-presence", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["device_status"] == "online"
