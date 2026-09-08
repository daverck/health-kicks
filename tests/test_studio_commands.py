"""Unit tests for remote Studio session start dispatch via AWS IoT Core."""

import json
from unittest.mock import MagicMock
import uuid

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.api.v1.telemetry import create_telemetry_router
from app.core.config import Settings
from app.db.models import Base, User, UserRole
from app.main import app as main_app
from app.services.iot_service import IotCommandService
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
def mock_iot_client():
    return MagicMock()


@pytest.fixture()
def iot_service(mock_iot_client):
    cfg = Settings(
        aws_iot_studio_start_topic="healthkicks/v1/{device_id}/commands/studio/start",
    )
    return IotCommandService(client=mock_iot_client, config=cfg)


@pytest.fixture()
def test_client(auth_user, iot_service) -> TestClient:
    app = FastAPI()
    app.include_router(create_telemetry_router(service=MagicMock(spec=TelemetryService), iot_service=iot_service))

    def override_get_current_user():
        return auth_user

    app.dependency_overrides[get_current_user] = override_get_current_user
    return TestClient(app)


def test_start_studio_session_success_defaults(test_client, mock_iot_client) -> None:
    device_id = "shoe-test-001"
    response = test_client.post(
        f"/api/v1/devices/{device_id}/commands/studio/start",
        json={"label": "walk"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "command_dispatched"
    assert data["device_id"] == device_id
    assert data["label"] == "walk"
    assert data["duration_sec"] == 5.0
    assert data["topic"] == f"healthkicks/v1/{device_id}/commands/studio/start"

    # Validate session_id is a valid UUID4
    session_id = data["session_id"]
    parsed_uuid = uuid.UUID(session_id)
    assert parsed_uuid.version == 4

    # Validate mock publish call
    mock_iot_client.publish.assert_called_once()
    call_kwargs = mock_iot_client.publish.call_args.kwargs
    assert call_kwargs["topic"] == f"healthkicks/v1/{device_id}/commands/studio/start"
    assert call_kwargs["qos"] == 1

    payload = json.loads(call_kwargs["payload"])
    assert payload["session_id"] == session_id
    assert payload["label"] == "walk"
    assert payload["duration_sec"] == 5.0
    assert payload["pulse_count"] == 3
    assert payload["pulse_duration_ms"] == 150
    assert payload["pulse_pause_ms"] == 350
    assert payload["pulse_intensity"] == 210


def test_start_studio_session_success_custom_parameters(test_client, mock_iot_client) -> None:
    device_id = "shoe-test-002"
    custom_payload = {
        "label": "fall_forward",
        "duration_sec": 10.0,
        "pulse_count": 4,
        "pulse_duration_ms": 200,
        "pulse_pause_ms": 400,
        "pulse_intensity": 220,
    }
    response = test_client.post(
        f"/api/v1/devices/{device_id}/commands/studio/start",
        json=custom_payload,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "command_dispatched"
    assert data["device_id"] == device_id
    assert data["label"] == "fall_forward"
    assert data["duration_sec"] == 10.0

    mock_iot_client.publish.assert_called_once()
    payload = json.loads(mock_iot_client.publish.call_args.kwargs["payload"])
    assert payload["session_id"] == data["session_id"]
    assert payload["label"] == "fall_forward"
    assert payload["duration_sec"] == 10.0
    assert payload["pulse_count"] == 4
    assert payload["pulse_duration_ms"] == 200
    assert payload["pulse_pause_ms"] == 400
    assert payload["pulse_intensity"] == 220


def test_start_studio_session_unauthenticated() -> None:
    with TestClient(main_app) as client:
        response = client.post(
            "/api/v1/devices/shoe-test-001/commands/studio/start",
            json={"label": "walk"},
        )
        assert response.status_code == 401


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {"label": ""},  # empty label
        {"label": "a" * 65},  # label > 64 chars
        {"label": "walk", "duration_sec": 0.5},  # duration < 1.0
        {"label": "walk", "duration_sec": 35.0},  # duration > 30.0
        {"label": "walk", "pulse_count": 0},  # count < 1
        {"label": "walk", "pulse_count": 6},  # count > 5
        {"label": "walk", "pulse_duration_ms": 40},  # duration < 50
        {"label": "walk", "pulse_duration_ms": 1001},  # duration > 1000
        {"label": "walk", "pulse_pause_ms": 99},  # pause < 100
        {"label": "walk", "pulse_pause_ms": 1001},  # pause > 1000
        {"label": "walk", "pulse_intensity": 40},  # intensity < 50
        {"label": "walk", "pulse_intensity": 256},  # intensity > 255
        {"label": "walk", "unexpected_field": "bad"},  # extra forbidden field
    ],
)
def test_start_studio_session_validation_errors(test_client, invalid_payload) -> None:
    response = test_client.post(
        "/api/v1/devices/shoe-test-001/commands/studio/start",
        json=invalid_payload,
    )
    assert response.status_code == 422


def test_start_studio_session_client_error(test_client, mock_iot_client) -> None:
    mock_iot_client.publish.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "Topic not found"}},
        "publish",
    )
    response = test_client.post(
        "/api/v1/devices/shoe-test-001/commands/studio/start",
        json={"label": "walk"},
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "Failed to dispatch command to device"


def test_start_studio_session_botocore_error(test_client, mock_iot_client) -> None:
    mock_iot_client.publish.side_effect = BotoCoreError()
    response = test_client.post(
        "/api/v1/devices/shoe-test-001/commands/studio/start",
        json={"label": "walk"},
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "Failed to dispatch command to device"


def test_start_studio_session_unexpected_error(test_client, mock_iot_client) -> None:
    mock_iot_client.publish.side_effect = RuntimeError("AWS IoT network timeout")
    response = test_client.post(
        "/api/v1/devices/shoe-test-001/commands/studio/start",
        json={"label": "walk"},
    )
    assert response.status_code == 502
    assert response.json()["detail"] == "Failed to dispatch command to device"
