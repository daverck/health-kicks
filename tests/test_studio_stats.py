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
from app.db.database import get_db
from app.db.models import Base, StudioSession, User, UserRole
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
def admin_user(db_session) -> User:
    user = User(
        google_sub="test-sub-admin",
        email="admin@example.com",
        name="Admin",
        role=UserRole.admin,
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
def test_client(auth_user, telemetry_service, db_session) -> TestClient:
    app = FastAPI()
    app.include_router(create_telemetry_router(service=telemetry_service))

    def override_get_current_user():
        return auth_user

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_db] = override_get_db
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


def test_get_dataset_stats_with_idle_label(telemetry_service, mock_table) -> None:
    """Vérifie que le label 'idle' est correctement comptabilisé dans les statistiques."""
    items = [
        {"session_id": "sess-idle-1", "label": "idle"},
        {"session_id": "sess-idle-1", "label": "idle"},
        {"session_id": "sess-idle-2", "label": "idle"},
        {"session_id": "sess-walk-1", "label": "walk"},
    ]
    mock_table.query.return_value = {"Items": items}

    stats = telemetry_service.get_dataset_stats(device_id="device-idle")

    assert stats.total_sessions == 3
    assert stats.by_label["idle"] == 2
    assert stats.by_label["walk"] == 1


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
# REST Endpoints Tests (PostgreSQL Aurora backed)
# ---------------------------------------------------------------------------


def test_get_device_studio_stats_endpoint(test_client, mock_table) -> None:
    mock_table.query.return_value = {
        "Items": [
            {"session_id": "s1", "label": "walk"},
            {"session_id": "s1", "label": "walk"},
            {"session_id": "s2", "label": "stumble"},
        ]
    }
def test_get_device_studio_stats_endpoint(test_client, auth_user, db_session) -> None:
    import uuid
    s1 = StudioSession(id=uuid.uuid4(), user_id=auth_user.id, device_id="shoe-123", label="walk", duration_sec=5.0, sample_count=50)
    s2 = StudioSession(id=uuid.uuid4(), user_id=auth_user.id, device_id="shoe-123", label="stumble", duration_sec=10.0, sample_count=100)
    s3 = StudioSession(id=uuid.uuid4(), user_id=auth_user.id, device_id="shoe-999", label="walk", duration_sec=7.0, sample_count=70)
    db_session.add_all([s1, s2, s3])
    db_session.commit()

    response = test_client.get("/api/v1/devices/shoe-123/studio/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["device_id"] == "shoe-123"
    assert data["total_sessions"] == 2
    assert data["total_duration_sec"] == 15.0
    assert data["total_samples"] == 150
    assert data["by_label"] == {"walk": 1, "stumble": 1}


def test_get_global_studio_stats_endpoint(test_client, mock_table) -> None:
    mock_table.scan.return_value = {
        "Items": [
            {"session_id": "s1", "label": "walk"},
            {"session_id": "s2", "label": "walk"},
            {"session_id": "s3", "label": "run"},
        ]
    }
def test_get_global_studio_stats_endpoint(test_client, auth_user, db_session) -> None:
    import uuid
    s1 = StudioSession(id=uuid.uuid4(), user_id=auth_user.id, device_id="shoe-1", label="walk", duration_sec=5.0, sample_count=50)
    s2 = StudioSession(id=uuid.uuid4(), user_id=auth_user.id, device_id="shoe-2", label="walk", duration_sec=5.0, sample_count=50)
    s3 = StudioSession(id=uuid.uuid4(), user_id=auth_user.id, device_id="shoe-3", label="run", duration_sec=10.0, sample_count=100)
    db_session.add_all([s1, s2, s3])
    db_session.commit()

    response = test_client.get("/api/v1/studio/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["device_id"] is None
    assert data["total_sessions"] == 3
    assert data["total_duration_sec"] == 20.0
    assert data["total_samples"] == 200
    assert data["by_label"] == {"walk": 2, "run": 1}


def test_studio_stats_rbac_strict_isolation(db_session, auth_user, admin_user) -> None:
    """Non-admin only sees their own sessions, admin sees all sessions."""
    import uuid
    s_clinician = StudioSession(id=uuid.uuid4(), user_id=auth_user.id, device_id="dev-1", label="walk", duration_sec=5.0, sample_count=50)
    s_other = StudioSession(id=uuid.uuid4(), user_id=admin_user.id, device_id="dev-2", label="idle", duration_sec=10.0, sample_count=100)
    db_session.add_all([s_clinician, s_other])
    db_session.commit()

    # Clinician client
    app = FastAPI()
    app.include_router(create_telemetry_router())
    app.dependency_overrides[get_current_user] = lambda: auth_user
    app.dependency_overrides[get_db] = lambda: db_session
    client_clinician = TestClient(app)

    res_clinician = client_clinician.get("/api/v1/studio/stats")
    assert res_clinician.status_code == 200
    data_clinician = res_clinician.json()
    assert data_clinician["total_sessions"] == 1
    assert data_clinician["by_label"] == {"walk": 1}
    assert data_clinician["total_duration_sec"] == 5.0

    # Admin client
    app.dependency_overrides[get_current_user] = lambda: admin_user
    client_admin = TestClient(app)

    res_admin = client_admin.get("/api/v1/studio/stats")
    assert res_admin.status_code == 200
    data_admin = res_admin.json()
    assert data_admin["total_sessions"] == 2
    assert data_admin["by_label"] == {"walk": 1, "idle": 1}
    assert data_admin["total_duration_sec"] == 15.0


def test_studio_stats_endpoints_unauthenticated(mock_table, db_session) -> None:
    app = FastAPI()
    app.include_router(create_telemetry_router(service=TelemetryService(table=mock_table)))
    app.dependency_overrides[get_db] = lambda: db_session
    unauth_client = TestClient(app)

    res_device = unauth_client.get("/api/v1/devices/shoe-123/studio/stats")
    assert res_device.status_code == 401

    res_global = unauth_client.get("/api/v1/studio/stats")
    assert res_global.status_code == 401


