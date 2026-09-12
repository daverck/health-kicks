"""Unit tests for Studio sessions history, RBAC, and curation endpoints."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock
import uuid
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.api.v1.studio_sessions import create_studio_sessions_router
from app.db.database import get_db
from app.db.models import Base, StudioSession, User, UserRole
from app.schemas.telemetry import ImuReadingResponse, StudioSessionReadingsResponse
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
def user_a(db_session) -> User:
    user = User(
        google_sub="sub-user-a",
        email="alice@example.com",
        name="Alice",
        role=UserRole.user,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def user_b(db_session) -> User:
    user = User(
        google_sub="sub-user-b",
        email="bob@example.com",
        name="Bob",
        role=UserRole.user,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def admin_user(db_session) -> User:
    admin = User(
        google_sub="sub-admin",
        email="admin@example.com",
        name="Admin",
        role=UserRole.admin,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin


@pytest.fixture()
def mock_telemetry_service():
    return MagicMock(spec=TelemetryService)


@pytest.fixture()
def client(db_session, mock_telemetry_service) -> TestClient:
    app = FastAPI()
    app.include_router(create_studio_sessions_router(service=mock_telemetry_service))

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _auth_headers(user: User) -> dict[str, str]:
    token = token_service.issue_access_token(user)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# RBAC and Listing Tests (GET /api/v1/studio/sessions)
# ---------------------------------------------------------------------------


def test_list_sessions_user_isolation(client, user_a, user_b, db_session) -> None:
    # 2 sessions for User A, 1 session for User B
    s_a1 = StudioSession(id=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk", sample_count=500)
    s_a2 = StudioSession(id=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="run", sample_count=450)
    s_b1 = StudioSession(id=uuid.uuid4(), user_id=user_b.id, device_id="HK-2", label="fall_forward", sample_count=300)
    db_session.add_all([s_a1, s_a2, s_b1])
    db_session.commit()

    # User A only sees their own 2 sessions, user_email is None
    res_a = client.get("/api/v1/studio/sessions", headers=_auth_headers(user_a))
    assert res_a.status_code == 200
    data_a = res_a.json()
    assert data_a["total"] == 2
    assert len(data_a["items"]) == 2
    for item in data_a["items"]:
        assert item["user_id"] == user_a.id
        assert item["user_email"] is None

    # User B only sees their 1 session
    res_b = client.get("/api/v1/studio/sessions", headers=_auth_headers(user_b))
    assert res_b.status_code == 200
    data_b = res_b.json()
    assert data_b["total"] == 1
    assert data_b["items"][0]["id"] == str(s_b1.id)
    assert data_b["items"][0]["user_email"] is None


def test_list_sessions_admin_sees_all_and_user_emails(client, admin_user, user_a, user_b, db_session) -> None:
    s_a = StudioSession(id=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk")
    s_b = StudioSession(id=uuid.uuid4(), user_id=user_b.id, device_id="HK-2", label="jump")
    db_session.add_all([s_a, s_b])
    db_session.commit()

    res = client.get("/api/v1/studio/sessions", headers=_auth_headers(admin_user))
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 2
    emails = {item["user_email"] for item in data["items"]}
    assert "alice@example.com" in emails
    assert "bob@example.com" in emails


def test_list_sessions_admin_filtering_by_user_id(client, admin_user, user_a, user_b, db_session) -> None:
    s_a = StudioSession(id=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk")
    s_b = StudioSession(id=uuid.uuid4(), user_id=user_b.id, device_id="HK-2", label="jump")
    db_session.add_all([s_a, s_b])
    db_session.commit()

    res = client.get(f"/api/v1/studio/sessions?user_id={user_a.id}", headers=_auth_headers(admin_user))
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == str(s_a.id)


def test_list_sessions_filtering_and_pagination(client, user_a, db_session) -> None:
    s1 = StudioSession(id=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    s2 = StudioSession(id=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="run", created_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    s3 = StudioSession(id=uuid.uuid4(), user_id=user_a.id, device_id="HK-2", label="walk", created_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
    db_session.add_all([s1, s2, s3])
    db_session.commit()

    # Filter by label
    res_lbl = client.get("/api/v1/studio/sessions?label=walk", headers=_auth_headers(user_a))
    assert res_lbl.json()["total"] == 2

    # Filter by device
    res_dev = client.get("/api/v1/studio/sessions?device_id=HK-2", headers=_auth_headers(user_a))
    assert res_dev.json()["total"] == 1
    assert res_dev.json()["items"][0]["id"] == str(s3.id)

    # Pagination: size=1
    res_p1 = client.get("/api/v1/studio/sessions?page=1&size=1", headers=_auth_headers(user_a))
    assert res_p1.json()["total"] == 3
    assert len(res_p1.json()["items"]) == 1
    # Check default sorting created_at DESC -> s3 is first
    assert res_p1.json()["items"][0]["id"] == str(s3.id)


def test_list_sessions_filter_by_date_range(client, user_a, db_session) -> None:
    s1 = StudioSession(id=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk", created_at=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc))
    s2 = StudioSession(id=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="run", created_at=datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc))
    s3 = StudioSession(id=uuid.uuid4(), user_id=user_a.id, device_id="HK-2", label="walk", created_at=datetime(2026, 5, 3, 18, 0, tzinfo=timezone.utc))
    db_session.add_all([s1, s2, s3])
    db_session.commit()

    # Filter with date-only end_date covering all of May 2
    res = client.get(
        "/api/v1/studio/sessions?start_date=2026-05-02&end_date=2026-05-02",
        headers=_auth_headers(user_a),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == str(s2.id)

    # Filter full range May 1 to May 2
    res_range = client.get(
        "/api/v1/studio/sessions?start_date=2026-05-01T00:00:00Z&end_date=2026-05-02T23:59:59Z",
        headers=_auth_headers(user_a),
    )
    assert res_range.status_code == 200
    assert res_range.json()["total"] == 2


def test_list_sessions_invalid_date_range_400(client, user_a) -> None:
    res = client.get(
        "/api/v1/studio/sessions?start_date=2026-05-10&end_date=2026-05-01",
        headers=_auth_headers(user_a),
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "start_date must be before or equal to end_date"


# ---------------------------------------------------------------------------
# Readings Inspection Tests (GET /api/v1/studio/sessions/{session_id}/readings)
# ---------------------------------------------------------------------------


def test_get_session_readings_success(client, user_a, mock_telemetry_service, db_session) -> None:
    sess_id = uuid.uuid4()
    s = StudioSession(id=sess_id, user_id=user_a.id, device_id="HK-1", label="walk")
    db_session.add(s)
    db_session.commit()

    mock_telemetry_service.get_session_readings.return_value = StudioSessionReadingsResponse(
        device_id="HK-1",
        session_id=str(sess_id),
        label="walk",
        sample_count=2,
        readings=[
            ImuReadingResponse(
                timestamp_epoch_us=100,
                timestamp_iso=datetime.now(timezone.utc),
                ax=1.0, ay=0.0, az=9.8, gx=0.1, gy=0.0, gz=0.0,
            ),
            ImuReadingResponse(
                timestamp_epoch_us=200,
                timestamp_iso=datetime.now(timezone.utc),
                ax=1.1, ay=0.1, az=9.7, gx=0.2, gy=0.0, gz=0.0,
            ),
        ],
    )

    res = client.get(f"/api/v1/studio/sessions/{sess_id}/readings", headers=_auth_headers(user_a))
    assert res.status_code == 200
    assert res.json()["session_id"] == str(sess_id)
    assert res.json()["sample_count"] == 2
    mock_telemetry_service.get_session_readings.assert_called_once_with(
        device_id="HK-1",
        session_id=str(sess_id),
    )


def test_get_session_readings_forbidden_for_other_user(client, user_a, user_b, db_session) -> None:
    sess_id = uuid.uuid4()
    s = StudioSession(id=sess_id, user_id=user_a.id, device_id="HK-1", label="walk")
    db_session.add(s)
    db_session.commit()

    res = client.get(f"/api/v1/studio/sessions/{sess_id}/readings", headers=_auth_headers(user_b))
    assert res.status_code == 403


def test_get_session_readings_admin_allowed(client, admin_user, user_a, mock_telemetry_service, db_session) -> None:
    sess_id = uuid.uuid4()
    s = StudioSession(id=sess_id, user_id=user_a.id, device_id="HK-1", label="walk")
    db_session.add(s)
    db_session.commit()

    mock_telemetry_service.get_session_readings.return_value = None

    res = client.get(f"/api/v1/studio/sessions/{sess_id}/readings", headers=_auth_headers(admin_user))
    assert res.status_code == 200
    assert res.json()["sample_count"] == 0


def test_get_session_readings_not_found(client, user_a) -> None:
    fake_id = uuid.uuid4()
    res = client.get(f"/api/v1/studio/sessions/{fake_id}/readings", headers=_auth_headers(user_a))
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Label Update Tests (PATCH /api/v1/studio/sessions/{session_id})
# ---------------------------------------------------------------------------


def test_patch_session_label_success(client, user_a, mock_telemetry_service, db_session) -> None:
    sess_id = uuid.uuid4()
    s = StudioSession(id=sess_id, user_id=user_a.id, device_id="HK-1", label="walk")
    db_session.add(s)
    db_session.commit()

    res = client.patch(
        f"/api/v1/studio/sessions/{sess_id}",
        headers=_auth_headers(user_a),
        json={"label": "stumble_recover"},
    )
    assert res.status_code == 200
    assert res.json()["label"] == "stumble_recover"

    # Verify DB update
    db_session.refresh(s)
    assert s.label == "stumble_recover"

    # Verify DynamoDB update call
    mock_telemetry_service.update_session_label.assert_called_once_with(
        device_id="HK-1",
        session_id=str(sess_id),
        new_label="stumble_recover",
    )


def test_patch_session_forbidden_for_other_user(client, user_a, user_b, db_session) -> None:
    sess_id = uuid.uuid4()
    s = StudioSession(id=sess_id, user_id=user_a.id, device_id="HK-1", label="walk")
    db_session.add(s)
    db_session.commit()

    res = client.patch(
        f"/api/v1/studio/sessions/{sess_id}",
        headers=_auth_headers(user_b),
        json={"label": "run"},
    )
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# Session Deletion Tests (DELETE /api/v1/studio/sessions/{session_id})
# ---------------------------------------------------------------------------


def test_delete_session_success(client, user_a, mock_telemetry_service, db_session) -> None:
    sess_id = uuid.uuid4()
    s = StudioSession(id=sess_id, user_id=user_a.id, device_id="HK-1", label="walk")
    db_session.add(s)
    db_session.commit()

    res = client.delete(
        f"/api/v1/studio/sessions/{sess_id}",
        headers=_auth_headers(user_a),
    )
    assert res.status_code == 204

    # Verify removed from PostgreSQL
    assert db_session.query(StudioSession).filter_by(id=sess_id).one_or_none() is None

    # Verify DynamoDB purge called
    mock_telemetry_service.delete_session_readings.assert_called_once_with(
        device_id="HK-1",
        session_id=str(sess_id),
    )


def test_delete_session_forbidden_for_other_user(client, user_a, user_b, db_session) -> None:
    sess_id = uuid.uuid4()
    s = StudioSession(id=sess_id, user_id=user_a.id, device_id="HK-1", label="walk")
    db_session.add(s)
    db_session.commit()

    res = client.delete(
        f"/api/v1/studio/sessions/{sess_id}",
        headers=_auth_headers(user_b),
    )
    assert res.status_code == 403
    assert db_session.query(StudioSession).filter_by(id=sess_id).one_or_none() is not None


def test_unauthenticated_rejected(client) -> None:
    assert client.get("/api/v1/studio/sessions").status_code == 401
    assert client.get(f"/api/v1/studio/sessions/{uuid.uuid4()}/readings").status_code == 401
    assert client.patch(f"/api/v1/studio/sessions/{uuid.uuid4()}", json={"label": "walk"}).status_code == 401
    assert client.delete(f"/api/v1/studio/sessions/{uuid.uuid4()}").status_code == 401


def test_idle_session_lifecycle_and_filtering(client, user_a, mock_telemetry_service, db_session) -> None:
    """Vérifie qu'une session avec le label 'idle' est acceptée, filtrée, paginée et modifiable."""
    sess_id = uuid.uuid4()
    s = StudioSession(
        id=sess_id,
        user_id=user_a.id,
        device_id="HK-1",
        label="idle",
        sample_count=50,
        duration_sec=5.0,
    )
    db_session.add(s)
    db_session.commit()

    # 1. Listing avec filtre ?label=idle
    res_idle = client.get("/api/v1/studio/sessions?label=idle", headers=_auth_headers(user_a))
    assert res_idle.status_code == 200
    data_idle = res_idle.json()
    assert data_idle["total"] == 1
    assert data_idle["items"][0]["id"] == str(sess_id)
    assert data_idle["items"][0]["label"] == "idle"

    # 2. Listing avec filtre ?label=walk (ne doit pas inclure idle)
    res_walk = client.get("/api/v1/studio/sessions?label=walk", headers=_auth_headers(user_a))
    assert res_walk.status_code == 200
    assert res_walk.json()["total"] == 0

    # 3. Modification du label vers 'stairs'
    res_patch = client.patch(
        f"/api/v1/studio/sessions/{sess_id}",
        json={"label": "stairs"},
        headers=_auth_headers(user_a),
    )
    assert res_patch.status_code == 200
    assert res_patch.json()["label"] == "stairs"

    # 4. Modification de retour vers 'idle'
    res_patch_idle = client.patch(
        f"/api/v1/studio/sessions/{sess_id}",
        json={"label": "idle"},
        headers=_auth_headers(user_a),
    )
    assert res_patch_idle.status_code == 200
    assert res_patch_idle.json()["label"] == "idle"

    # Vérification en base
    db_session.refresh(s)
    assert s.label == "idle"
