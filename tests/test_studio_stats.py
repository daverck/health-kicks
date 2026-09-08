"""Unit tests for Studio dataset statistics service and REST endpoints."""

from unittest.mock import MagicMock
from botocore.exceptions import ClientError
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.api.v1.telemetry import create_telemetry_router
from app.db.models import Base, User, UserRole
from app.schemas.telemetry import StudioDatasetStatsResponse
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
        google_sub="test-sub-stats",
        email="researcher@example.com",
        name="Researcher",
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
    return MagicMock()


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
# TelemetryService.get_dataset_stats Tests
# ---------------------------------------------------------------------------


def test_get_dataset_stats_aggregates_and_deduplicates(telemetry_service, mock_table) -> None:
    # 500 frames for sess-1 ("walk"), 200 frames for sess-2 ("walk"), 100 frames for sess-3 ("fall_forward")
    items = []
    for _ in range(500):
        items.append({"session_id": "sess-1", "label": "walk"})
    for _ in range(200):
        items.append({"session_id": "sess-2", "label": "walk"})
    for _ in range(100):
        items.append({"session_id": "sess-3", "label": "fall_forward"})

    mock_table.query.return_value = {"Items": items}

    stats = telemetry_service.get_dataset_stats(device_id="device-42")

    assert stats.device_id == "device-42"
    assert stats.total_sessions == 3
    assert stats.by_label == {"walk": 2, "fall_forward": 1}

    # Verify query was called with projection and expression attribute names
    mock_table.query.assert_called_once()
    call_kwargs = mock_table.query.call_args[1]
    assert call_kwargs["ProjectionExpression"] == "session_id, #lbl"
    assert call_kwargs["ExpressionAttributeNames"] == {"#lbl": "label"}


def test_get_dataset_stats_empty(telemetry_service, mock_table) -> None:
    mock_table.query.return_value = {"Items": []}

    stats = telemetry_service.get_dataset_stats(device_id="device-empty")

    assert stats.device_id == "device-empty"
    assert stats.total_sessions == 0
    assert stats.by_label == {}


def test_get_dataset_stats_pagination(telemetry_service, mock_table) -> None:
    mock_table.query.side_effect = [
        {
            "Items": [
                {"session_id": "sess-1", "label": "walk"},
                {"session_id": "sess-2", "label": "run"},
            ],
            "LastEvaluatedKey": {"device_id": "device-1", "timestamp": 12345},
        },
        {
            "Items": [
                {"session_id": "sess-2", "label": "run"},  # duplicate in next page
                {"session_id": "sess-3", "label": "jump"},
            ],
        },
    ]

    stats = telemetry_service.get_dataset_stats(device_id="device-1")

    assert mock_table.query.call_count == 2
    assert stats.total_sessions == 3
    assert stats.by_label == {"walk": 1, "run": 1, "jump": 1}


def test_get_dataset_stats_global_uses_scan(telemetry_service, mock_table) -> None:
    mock_table.scan.return_value = {
        "Items": [
            {"session_id": "sess-1", "label": "walk"},
            {"session_id": "sess-2", "label": "fall_backward"},
        ]
    }

    stats = telemetry_service.get_dataset_stats(device_id=None)

    assert stats.device_id is None
    assert stats.total_sessions == 2
    assert stats.by_label == {"walk": 1, "fall_backward": 1}
    mock_table.scan.assert_called_once()
    mock_table.query.assert_not_called()


def test_get_dataset_stats_raises_on_dynamo_error(telemetry_service, mock_table) -> None:
    mock_table.query.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Throttled"}},
        "Query",
    )

    with pytest.raises(ClientError):
        telemetry_service.get_dataset_stats(device_id="device-1")


# ---------------------------------------------------------------------------
# REST Endpoints Tests
# ---------------------------------------------------------------------------


def test_get_device_studio_stats_endpoint(test_client, mock_table) -> None:
    mock_table.query.return_value = {
        "Items": [
            {"session_id": "s1", "label": "walk"},
            {"session_id": "s1", "label": "walk"},
            {"session_id": "s2", "label": "stumble"},
        ]
    }

    response = test_client.get("/api/v1/devices/shoe-123/studio/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["device_id"] == "shoe-123"
    assert data["total_sessions"] == 2
    assert data["by_label"] == {"walk": 1, "stumble": 1}


def test_get_global_studio_stats_endpoint(test_client, mock_table) -> None:
    mock_table.scan.return_value = {
        "Items": [
            {"session_id": "s1", "label": "walk"},
            {"session_id": "s2", "label": "walk"},
            {"session_id": "s3", "label": "run"},
        ]
    }

    response = test_client.get("/api/v1/studio/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["device_id"] is None
    assert data["total_sessions"] == 3
    assert data["by_label"] == {"walk": 2, "run": 1}


def test_studio_stats_endpoints_unauthenticated(mock_table) -> None:
    app = FastAPI()
    app.include_router(create_telemetry_router(service=TelemetryService(table=mock_table)))
    unauth_client = TestClient(app)

    res_device = unauth_client.get("/api/v1/devices/shoe-123/studio/stats")
    assert res_device.status_code == 401

    res_global = unauth_client.get("/api/v1/studio/stats")
    assert res_global.status_code == 401


def test_studio_stats_endpoint_error_handling(test_client, mock_table) -> None:
    mock_table.query.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "DynamoDB Error"}},
        "Query",
    )
    mock_table.scan.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "DynamoDB Error"}},
        "Scan",
    )

    res_device = test_client.get("/api/v1/devices/shoe-123/studio/stats")
    assert res_device.status_code == 502
    assert res_device.json()["detail"] == "Failed to retrieve studio stats from telemetry store"

    res_global = test_client.get("/api/v1/studio/stats")
    assert res_global.status_code == 502
    assert res_global.json()["detail"] == "Failed to retrieve studio stats from telemetry store"

