"""Tests for DynamoDB IMU telemetry service and REST endpoints."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.api.v1.telemetry import create_telemetry_router
from app.db.database import get_db
from app.db.models import Base, DeviceOwnership, User, UserRole
from app.services import token_service
from app.services.telemetry_service import TelemetryService


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
def auth_user(db_session) -> User:
    user = User(
        google_sub="test-sub-1",
        email="clinician@example.com",
        name="Clinician",
        role=UserRole.clinician,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def auth_headers(auth_user) -> dict[str, str]:
    token = token_service.issue_access_token(auth_user)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def mock_table():
    table = MagicMock()
    return table


@pytest.fixture()
def telemetry_service(mock_table):
    return TelemetryService(table=mock_table)


@pytest.fixture()
def test_client(auth_user, telemetry_service) -> TestClient:
    app = FastAPI()
    app.include_router(create_telemetry_router(service=telemetry_service))

    def override_get_current_user():
        return auth_user

    app.dependency_overrides[get_current_user] = override_get_current_user
    return TestClient(app)


# ---------------------------------------------------------------------------
# TelemetryService Unit Tests
# ---------------------------------------------------------------------------


def test_get_session_readings_success(telemetry_service, mock_table) -> None:
    items = [
        {
            "device_id": "HK-1",
            "timestamp": 1700000000020000,
            "ax": Decimal("0.15"),
            "ay": Decimal("-0.30"),
            "az": Decimal("9.85"),
            "gx": Decimal("0.02"),
            "gy": Decimal("-0.01"),
            "gz": Decimal("0.04"),
            "session_id": "sess-1",
            "label": "fall_forward",
        },
        {
            "device_id": "HK-1",
            "timestamp": 1700000000000000,
            "ax": Decimal("0.12"),
            "ay": Decimal("-0.34"),
            "az": Decimal("9.81"),
            "gx": Decimal("0.01"),
            "gy": Decimal("-0.02"),
            "gz": Decimal("0.05"),
            "session_id": "sess-1",
            "label": "fall_forward",
        },
    ]
    mock_table.query.return_value = {"Items": items}

    response = telemetry_service.get_session_readings(device_id="HK-1", session_id="sess-1")
    assert response is not None
    assert response.device_id == "HK-1"
    assert response.session_id == "sess-1"
    assert response.label == "fall_forward"
    assert response.sample_count == 2
    # Verify sorting: 1700000000000000 comes before 1700000000020000
    assert response.readings[0].timestamp_epoch_us == 1700000000000000
    assert response.readings[1].timestamp_epoch_us == 1700000000020000
    # Verify Decimal to float conversion
    assert isinstance(response.readings[0].ax, float)
    assert response.readings[0].ax == 0.12
    assert response.readings[0].az == 9.81
    # Verify timestamp ISO
    assert response.readings[0].timestamp_iso == datetime.fromtimestamp(
        1700000000, tz=UTC
    )


def test_get_session_readings_pagination(telemetry_service, mock_table) -> None:
    first_page = {
        "Items": [
            {
                "device_id": "HK-1",
                "timestamp": 1700000000000000,
                "ax": Decimal("0.10"),
                "ay": Decimal("0.20"),
                "az": Decimal("9.80"),
                "gx": Decimal("0.0"),
                "gy": Decimal("0.0"),
                "gz": Decimal("0.0"),
                "session_id": "sess-p",
                "label": "walk",
            }
        ],
        "LastEvaluatedKey": {"device_id": "HK-1", "timestamp": 1700000000000000},
    }
    second_page = {
        "Items": [
            {
                "device_id": "HK-1",
                "timestamp": 1700000000010000,
                "ax": Decimal("0.11"),
                "ay": Decimal("0.21"),
                "az": Decimal("9.81"),
                "gx": Decimal("0.0"),
                "gy": Decimal("0.0"),
                "gz": Decimal("0.0"),
                "session_id": "sess-p",
                "label": "walk",
            }
        ]
    }
    mock_table.query.side_effect = [first_page, second_page]

    response = telemetry_service.get_session_readings("HK-1", "sess-p")
    assert response is not None
    assert response.sample_count == 2
    assert mock_table.query.call_count == 2


def test_get_session_readings_not_found(telemetry_service, mock_table) -> None:
    mock_table.query.return_value = {"Items": []}
    assert telemetry_service.get_session_readings("HK-1", "missing-session") is None


def test_get_timerange_readings(telemetry_service, mock_table) -> None:
    mock_table.query.return_value = {
        "Items": [
            {
                "device_id": "HK-1",
                "timestamp": 1700000000000000,
                "ax": Decimal("0.12"),
                "ay": Decimal("0.34"),
                "az": Decimal("9.81"),
                "gx": Decimal("0.01"),
                "gy": Decimal("0.02"),
                "gz": Decimal("0.03"),
            }
        ]
    }

    readings = telemetry_service.get_timerange_readings(
        device_id="HK-1",
        start_epoch_us=1700000000000000,
        end_epoch_us=1700000001000000,
        limit=50,
    )
    assert len(readings) == 1
    assert readings[0].ax == 0.12
    assert readings[0].timestamp_epoch_us == 1700000000000000
    mock_table.query.assert_called_once()
    assert mock_table.query.call_args[1]["Limit"] == 50


def test_delete_session_readings(telemetry_service, mock_table) -> None:
    mock_table.query.return_value = {
        "Items": [
            {"device_id": "HK-1", "timestamp": 100},
            {"device_id": "HK-1", "timestamp": 200},
            {"device_id": "HK-1", "timestamp": 300},
        ]
    }
    batch_mock = MagicMock()
    mock_table.batch_writer.return_value.__enter__.return_value = batch_mock

    deleted_count = telemetry_service.delete_session_readings("HK-1", "sess-delete")
    assert deleted_count == 3
    assert batch_mock.delete_item.call_count == 3
    batch_mock.delete_item.assert_any_call(Key={"device_id": "HK-1", "timestamp": 100})
    batch_mock.delete_item.assert_any_call(Key={"device_id": "HK-1", "timestamp": 200})
    batch_mock.delete_item.assert_any_call(Key={"device_id": "HK-1", "timestamp": 300})


def test_delete_session_readings_empty(telemetry_service, mock_table) -> None:
    mock_table.query.return_value = {"Items": []}
    deleted = telemetry_service.delete_session_readings("HK-1", "empty-session")
    assert deleted == 0
    mock_table.batch_writer.assert_not_called()


def test_trim_session_readings_deletes_only_frames_after_cutoff(
    telemetry_service, mock_table
) -> None:
    mock_table.query.return_value = {
        "Items": [
            {"device_id": "HK-1", "timestamp": 2_500_001},
            {"device_id": "HK-1", "timestamp": 1_000_000},
            {"device_id": "HK-1", "timestamp": 2_000_000},
            {"device_id": "HK-1", "timestamp": 1_500_000},
        ]
    }
    batch_mock = MagicMock()
    mock_table.batch_writer.return_value.__enter__.return_value = batch_mock

    retained_count = telemetry_service.trim_session_readings(
        "HK-1", "sess-trim", new_duration_sec=1.0
    )

    assert retained_count == 3
    batch_mock.delete_item.assert_called_once_with(
        Key={"device_id": "HK-1", "timestamp": 2_500_001}
    )


def test_trim_session_readings_empty(telemetry_service, mock_table) -> None:
    mock_table.query.return_value = {"Items": []}

    retained_count = telemetry_service.trim_session_readings(
        "HK-1", "empty-session", new_duration_sec=1.0
    )

    assert retained_count == 0
    mock_table.batch_writer.assert_not_called()


# ---------------------------------------------------------------------------
# API Route Tests
# ---------------------------------------------------------------------------


def test_telemetry_unauthenticated_rejected() -> None:
    from app.main import app

    client = TestClient(app)
    assert client.get("/api/v1/devices/HK-1/telemetry").status_code == 401
    assert client.delete("/api/v1/devices/HK-1/telemetry/sessions/sess-1").status_code == 401


def test_api_get_telemetry_by_session_success(test_client, mock_table) -> None:
    mock_table.query.return_value = {
        "Items": [
            {
                "device_id": "HK-1",
                "timestamp": 1700000000000000,
                "ax": Decimal("0.5"),
                "ay": Decimal("-0.5"),
                "az": Decimal("9.8"),
                "gx": Decimal("0.1"),
                "gy": Decimal("0.2"),
                "gz": Decimal("0.3"),
                "session_id": "sess-ok",
                "label": "fall",
            }
        ]
    }

    res = test_client.get("/api/v1/devices/HK-1/telemetry?session_id=sess-ok")
    assert res.status_code == 200
    data = res.json()
    assert data["device_id"] == "HK-1"
    assert data["session_id"] == "sess-ok"
    assert data["label"] == "fall"
    assert data["sample_count"] == 1
    assert len(data["readings"]) == 1
    assert data["readings"][0]["ax"] == 0.5
    assert data["readings"][0]["timestamp_epoch_us"] == 1700000000000000


def test_api_get_telemetry_by_session_not_found(test_client, mock_table) -> None:
    mock_table.query.return_value = {"Items": []}
    res = test_client.get("/api/v1/devices/HK-1/telemetry?session_id=sess-missing")
    assert res.status_code == 404
    assert "not found" in res.json()["detail"]


def test_api_get_telemetry_by_timerange_success(test_client, mock_table) -> None:
    mock_table.query.return_value = {
        "Items": [
            {
                "device_id": "HK-1",
                "timestamp": 1700000000000000,
                "ax": Decimal("0.1"),
                "ay": Decimal("0.2"),
                "az": Decimal("9.8"),
                "gx": Decimal("0.0"),
                "gy": Decimal("0.0"),
                "gz": Decimal("0.0"),
            }
        ]
    }

    start = "2023-11-14T22:13:20Z"
    end = "2023-11-14T22:13:30Z"
    res = test_client.get(f"/api/v1/devices/HK-1/telemetry?start_time={start}&end_time={end}&limit=100")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["timestamp_epoch_us"] == 1700000000000000


def test_api_get_telemetry_timerange_invalid_order(test_client) -> None:
    start = "2023-11-14T22:13:30Z"
    end = "2023-11-14T22:13:20Z"
    res = test_client.get(f"/api/v1/devices/HK-1/telemetry?start_time={start}&end_time={end}")
    assert res.status_code == 400
    assert "start_time must be less than or equal to end_time" in res.json()["detail"]


def test_api_get_telemetry_missing_params(test_client) -> None:
    res = test_client.get("/api/v1/devices/HK-1/telemetry")
    assert res.status_code == 400
    assert "Must provide either session_id or both start_time and end_time" in res.json()["detail"]


def test_api_delete_session_success(test_client, mock_table) -> None:
    mock_table.query.return_value = {
        "Items": [
            {"device_id": "HK-1", "timestamp": 100},
        ]
    }
    batch_mock = MagicMock()
    mock_table.batch_writer.return_value.__enter__.return_value = batch_mock

    res = test_client.delete("/api/v1/devices/HK-1/telemetry/sessions/sess-purge")
    assert res.status_code == 204
    assert res.text == ""
    assert batch_mock.delete_item.call_count == 1


def test_api_get_telemetry_temporal_isolation(db_session, telemetry_service, mock_table) -> None:
    """Ensure regular user telemetry queries are clamped to bound_at_utc and prior session telemetry is 404."""
    t_bound = datetime(2023, 11, 14, 22, 13, 25, tzinfo=UTC)
    regular_user = User(
        id=99,
        google_sub="test-sub-regular",
        email="regular@example.com",
        name="Regular",
        role=UserRole.user,
    )
    db_session.add(regular_user)
    db_session.add(DeviceOwnership(user_id=99, device_id="HK-BOUND", bound_at_utc=t_bound))
    db_session.commit()

    app = FastAPI()
    app.include_router(create_telemetry_router(service=telemetry_service))
    app.dependency_overrides[get_current_user] = lambda: regular_user
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)

    # 1. Range strictly before bound_at_utc returns empty list without querying DynamoDB
    start_prior = "2023-11-14T22:13:00Z"
    end_prior = "2023-11-14T22:13:20Z"
    res = client.get(f"/api/v1/devices/HK-BOUND/telemetry?start_time={start_prior}&end_time={end_prior}")
    assert res.status_code == 200
    assert res.json() == []

    # 2. Range overlapping bound_at_utc clamps start_time to bound_at_utc
    clamped_us = int(t_bound.timestamp() * 1_000_000)
    mock_table.query.return_value = {
        "Items": [
            {
                "device_id": "HK-BOUND",
                "timestamp": clamped_us + 1000,
                "ax": Decimal("0.1"),
                "ay": Decimal("0.2"),
                "az": Decimal("9.8"),
                "gx": Decimal("0.0"),
                "gy": Decimal("0.0"),
                "gz": Decimal("0.0"),
            }
        ]
    }
    start_str = "2023-11-14T22:13:20Z"
    end_str = "2023-11-14T22:13:30Z"
    from unittest.mock import patch
    with patch.object(telemetry_service, "get_timerange_readings", wraps=telemetry_service.get_timerange_readings) as spy:
        res2 = client.get(f"/api/v1/devices/HK-BOUND/telemetry?start_time={start_str}&end_time={end_str}")
        assert res2.status_code == 200
        assert len(res2.json()) == 1
        spy.assert_called_once_with(
            device_id="HK-BOUND",
            start_epoch_us=clamped_us,
            end_epoch_us=int(datetime(2023, 11, 14, 22, 13, 30, tzinfo=UTC).timestamp() * 1_000_000),
            limit=1000,
        )
