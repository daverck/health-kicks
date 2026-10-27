"""Unit tests for Studio sessions history, RBAC, and curation endpoints."""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

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
        role=UserRole.clinician,
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
        role=UserRole.clinician,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def regular_user(db_session) -> User:
    user = User(
        google_sub="sub-regular-user",
        email="regular@example.com",
        name="Regular",
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
    s_a1 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk", sample_count=500)
    s_a2 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="run", sample_count=450)
    s_b1 = StudioSession(uuid=uuid.uuid4(), user_id=user_b.id, device_id="HK-2", label="fall_forward", sample_count=300)
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
    assert data_b["items"][0]["uuid"] == str(s_b1.uuid)
    assert data_b["items"][0]["user_email"] is None


def test_list_sessions_admin_sees_all_with_pseudonymization(client, admin_user, user_a, user_b, db_session) -> None:
    s_a = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk")
    s_b = StudioSession(uuid=uuid.uuid4(), user_id=user_b.id, device_id="HK-2", label="jump")
    s_admin = StudioSession(uuid=uuid.uuid4(), user_id=admin_user.id, device_id="HK-3", label="idle")
    db_session.add_all([s_a, s_b, s_admin])
    db_session.commit()

    res = client.get("/api/v1/studio/sessions", headers=_auth_headers(admin_user))
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 3

    # Third-party sessions are strictly pseudonymized (no PII leak)
    item_a = next(i for i in data["items"] if i["uuid"] == str(s_a.uuid))
    assert item_a["user_email"] is None
    assert item_a["is_pseudonymized"] is True
    assert item_a["subject_pseudonym"].startswith("SUBJ-")

    item_b = next(i for i in data["items"] if i["uuid"] == str(s_b.uuid))
    assert item_b["user_email"] is None
    assert item_b["is_pseudonymized"] is True
    assert item_b["subject_pseudonym"].startswith("SUBJ-")

    # Admin's own session preserves their email and is not pseudonymized
    item_admin = next(i for i in data["items"] if i["uuid"] == str(s_admin.uuid))
    assert item_admin["user_email"] == admin_user.email
    assert item_admin["is_pseudonymized"] is False


def test_list_sessions_admin_filtering_by_pseudonym(client, admin_user, user_a, user_b, db_session) -> None:
    s_a = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk")
    s_b = StudioSession(uuid=uuid.uuid4(), user_id=user_b.id, device_id="HK-2", label="jump")
    db_session.add_all([s_a, s_b])
    db_session.commit()

    res = client.get(f"/api/v1/studio/sessions?pseudonym={user_a.pseudonym}", headers=_auth_headers(admin_user))
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["items"][0]["uuid"] == str(s_a.uuid)
    assert data["items"][0]["author_uuid"] == str(user_a.uuid)
    assert data["items"][0]["author_pseudonym"] == user_a.pseudonym


def test_list_sessions_filtering_and_pagination(client, user_a, db_session) -> None:
    s1 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    s2 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="run", created_at=datetime(2026, 1, 2, tzinfo=UTC))
    s3 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-2", label="walk", created_at=datetime(2026, 1, 3, tzinfo=UTC))
    db_session.add_all([s1, s2, s3])
    db_session.commit()

    # Filter by label
    res_lbl = client.get("/api/v1/studio/sessions?label=walk", headers=_auth_headers(user_a))
    assert res_lbl.json()["total"] == 2

    # Filter by device
    res_dev = client.get("/api/v1/studio/sessions?device_id=HK-2", headers=_auth_headers(user_a))
    assert res_dev.json()["total"] == 1
    assert res_dev.json()["items"][0]["uuid"] == str(s3.uuid)

    # Pagination: size=1
    res_p1 = client.get("/api/v1/studio/sessions?page=1&size=1", headers=_auth_headers(user_a))
    assert res_p1.json()["total"] == 3
    assert len(res_p1.json()["items"]) == 1
    # Check default sorting created_at DESC -> s3 is first
    assert res_p1.json()["items"][0]["uuid"] == str(s3.uuid)


def test_list_sessions_filter_by_date_range(client, user_a, db_session) -> None:
    s1 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk", created_at=datetime(2026, 5, 1, 10, 0, tzinfo=UTC))
    s2 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="run", created_at=datetime(2026, 5, 2, 14, 0, tzinfo=UTC))
    s3 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-2", label="walk", created_at=datetime(2026, 5, 3, 18, 0, tzinfo=UTC))
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
    assert data["items"][0]["uuid"] == str(s2.uuid)

    # Filter full range May 1 to May 2
    res_range = client.get(
        "/api/v1/studio/sessions?start_date=2026-05-01T00:00:00Z&end_date=2026-05-02T23:59:59Z",
        headers=_auth_headers(user_a),
    )
    assert res_range.status_code == 200
    assert res_range.json()["total"] == 2


def test_list_sessions_filter_by_sample_count_and_duration(client, user_a, db_session) -> None:
    short = StudioSession(
        uuid=uuid.uuid4(),
        user_id=user_a.id,
        device_id="HK-1",
        label="walk",
        sample_count=100,
        duration_sec=2.0,
    )
    matching = StudioSession(
        uuid=uuid.uuid4(),
        user_id=user_a.id,
        device_id="HK-1",
        label="run",
        sample_count=200,
        duration_sec=4.0,
    )
    long = StudioSession(
        uuid=uuid.uuid4(),
        user_id=user_a.id,
        device_id="HK-1",
        label="stairs",
        sample_count=300,
        duration_sec=6.0,
    )
    db_session.add_all([short, matching, long])
    db_session.commit()

    response = client.get(
        "/api/v1/studio/sessions"
        "?sample_count_min=150&sample_count_max=250&duration_sec_min=3&duration_sec_max=5",
        headers=_auth_headers(user_a),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert [item["uuid"] for item in data["items"]] == [str(matching.uuid)]


@pytest.mark.parametrize(
    ("query_params", "expected_detail"),
    [
        (
            "sample_count_min=20&sample_count_max=10",
            "sample_count_min must be less than or equal to sample_count_max",
        ),
        (
            "duration_sec_min=2.5&duration_sec_max=2",
            "duration_sec_min must be less than or equal to duration_sec_max",
        ),
    ],
)
def test_list_sessions_rejects_invalid_measurement_ranges(
    client, user_a, query_params, expected_detail
) -> None:
    response = client.get(
        f"/api/v1/studio/sessions?{query_params}",
        headers=_auth_headers(user_a),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == expected_detail


def test_list_sessions_invalid_date_range_400(client, user_a) -> None:
    res = client.get(
        "/api/v1/studio/sessions?start_date=2026-05-10&end_date=2026-05-01",
        headers=_auth_headers(user_a),
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "start_date must be before or equal to end_date"


def test_list_sessions_cursor_pagination(client, user_a, db_session) -> None:
    s1 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk", created_at=datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    s2 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="run", created_at=datetime(2026, 6, 2, 10, 0, tzinfo=UTC))
    s3 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="stairs", created_at=datetime(2026, 6, 3, 10, 0, tzinfo=UTC))
    db_session.add_all([s1, s2, s3])
    db_session.commit()

    # Page 1 with size 1
    res1 = client.get("/api/v1/studio/sessions?size=1", headers=_auth_headers(user_a))
    assert res1.status_code == 200
    data1 = res1.json()
    assert len(data1["items"]) == 1
    assert data1["items"][0]["uuid"] == str(s3.uuid)
    assert data1["next_cursor"] is not None

    # Page 2 via next_cursor
    cursor1 = data1["next_cursor"]
    res2 = client.get(f"/api/v1/studio/sessions?cursor={cursor1}&size=1", headers=_auth_headers(user_a))
    assert res2.status_code == 200
    data2 = res2.json()
    assert len(data2["items"]) == 1
    assert data2["items"][0]["uuid"] == str(s2.uuid)
    assert data2["next_cursor"] is not None

    # Page 3 via next_cursor
    cursor2 = data2["next_cursor"]
    res3 = client.get(f"/api/v1/studio/sessions?cursor={cursor2}&size=1", headers=_auth_headers(user_a))
    assert res3.status_code == 200
    data3 = res3.json()
    assert len(data3["items"]) == 1
    assert data3["items"][0]["uuid"] == str(s1.uuid)
    assert data3["next_cursor"] is None


def test_list_sessions_invalid_cursor_400(client, user_a) -> None:
    res = client.get(
        "/api/v1/studio/sessions?cursor=invalid-not-base64",
        headers=_auth_headers(user_a),
    )
    assert res.status_code == 400
    assert "Invalid pagination cursor" in res.json()["detail"]


# ---------------------------------------------------------------------------
# Readings Inspection Tests (GET /api/v1/studio/sessions/{session_id}/readings)
# ---------------------------------------------------------------------------


def test_get_session_readings_success(client, user_a, mock_telemetry_service, db_session) -> None:
    sess_id = uuid.uuid4()
    s = StudioSession(uuid=sess_id, user_id=user_a.id, device_id="HK-1", label="walk")
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
                timestamp_iso=datetime.now(UTC),
                ax=1.0, ay=0.0, az=9.8, gx=0.1, gy=0.0, gz=0.0,
            ),
            ImuReadingResponse(
                timestamp_epoch_us=200,
                timestamp_iso=datetime.now(UTC),
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
    s = StudioSession(uuid=sess_id, user_id=user_a.id, device_id="HK-1", label="walk")
    db_session.add(s)
    db_session.commit()

    res = client.get(f"/api/v1/studio/sessions/{sess_id}/readings", headers=_auth_headers(user_b))
    assert res.status_code == 403


def test_get_session_readings_admin_allowed(client, admin_user, user_a, mock_telemetry_service, db_session) -> None:
    sess_id = uuid.uuid4()
    s = StudioSession(uuid=sess_id, user_id=user_a.id, device_id="HK-1", label="walk")
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
    s = StudioSession(uuid=sess_id, user_id=user_a.id, device_id="HK-1", label="walk")
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
    s = StudioSession(uuid=sess_id, user_id=user_a.id, device_id="HK-1", label="walk")
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
    s = StudioSession(uuid=sess_id, user_id=user_a.id, device_id="HK-1", label="walk")
    db_session.add(s)
    db_session.commit()

    res = client.delete(
        f"/api/v1/studio/sessions/{sess_id}",
        headers=_auth_headers(user_a),
    )
    assert res.status_code == 204

    # Verify removed from PostgreSQL
    assert db_session.query(StudioSession).filter_by(uuid=sess_id).one_or_none() is None

    # Verify DynamoDB purge called
    mock_telemetry_service.delete_session_readings.assert_called_once_with(
        device_id="HK-1",
        session_id=str(sess_id),
    )


def test_delete_session_forbidden_for_other_user(client, user_a, user_b, db_session) -> None:
    sess_id = uuid.uuid4()
    s = StudioSession(uuid=sess_id, user_id=user_a.id, device_id="HK-1", label="walk")
    db_session.add(s)
    db_session.commit()

    res = client.delete(
        f"/api/v1/studio/sessions/{sess_id}",
        headers=_auth_headers(user_b),
    )
    assert res.status_code == 403
    assert db_session.query(StudioSession).filter_by(uuid=sess_id).one_or_none() is not None


def test_unauthenticated_rejected(client) -> None:
    assert client.get("/api/v1/studio/sessions").status_code == 401
    assert client.get(f"/api/v1/studio/sessions/{uuid.uuid4()}/readings").status_code == 401
    assert client.patch(f"/api/v1/studio/sessions/{uuid.uuid4()}", json={"label": "walk"}).status_code == 401
    assert client.delete(f"/api/v1/studio/sessions/{uuid.uuid4()}").status_code == 401


def test_idle_session_lifecycle_and_filtering(client, user_a, mock_telemetry_service, db_session) -> None:
    """Vérifie qu'une session avec le label 'idle' est acceptée, filtrée, paginée et modifiable."""
    sess_id = uuid.uuid4()
    s = StudioSession(
        uuid=sess_id,
        user_id=user_a.id,
        device_id="HK-1",
        label="idle",
        sample_count=50,
        duration_sec=5.0,
    )
    db_session.add(s)
    db_session.commit()

    # 1. Listing with filter ?label=idle
    res_idle = client.get("/api/v1/studio/sessions?label=idle", headers=_auth_headers(user_a))
    assert res_idle.status_code == 200
    data_idle = res_idle.json()
    assert data_idle["total"] == 1
    assert data_idle["items"][0]["uuid"] == str(sess_id)
    assert data_idle["items"][0]["label"] == "idle"

    # 2. Listing with filter ?label=walk (must not include idle)
    res_walk = client.get("/api/v1/studio/sessions?label=walk", headers=_auth_headers(user_a))
    assert res_walk.status_code == 200
    assert res_walk.json()["total"] == 0

    # 3. Relabel to 'stairs'
    res_patch = client.patch(
        f"/api/v1/studio/sessions/{sess_id}",
        json={"label": "stairs"},
        headers=_auth_headers(user_a),
    )
    assert res_patch.status_code == 200
    assert res_patch.json()["label"] == "stairs"

    # 4. Relabel back to 'idle'
    res_patch_idle = client.patch(
        f"/api/v1/studio/sessions/{sess_id}",
        json={"label": "idle"},
        headers=_auth_headers(user_a),
    )
    assert res_patch_idle.status_code == 200
    assert res_patch_idle.json()["label"] == "idle"

    # Database verification
    db_session.refresh(s)
    assert s.label == "idle"


# ---------------------------------------------------------------------------
# Session Validation / Confirmation Lifecycle Tests
# ---------------------------------------------------------------------------


def test_confirm_session_success(client, user_a, db_session) -> None:
    """A user can confirm/validate their own recorded session."""
    sess_id = uuid.uuid4()
    s = StudioSession(uuid=sess_id, user_id=user_a.id, device_id="HK-1", label="walk", is_validated=False)
    db_session.add(s)
    db_session.commit()

    res = client.patch(
        f"/api/v1/studio/sessions/{sess_id}/confirm",
        headers=_auth_headers(user_a),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["uuid"] == str(sess_id)
    assert data["is_validated"] is True

    db_session.refresh(s)
    assert s.is_validated is True


def test_update_session_can_revert_validation(client, user_a, db_session) -> None:
    sess_id = uuid.uuid4()
    session = StudioSession(
        uuid=sess_id,
        user_id=user_a.id,
        device_id="HK-1",
        label="walk",
        is_validated=True,
    )
    db_session.add(session)
    db_session.commit()

    response = client.patch(
        f"/api/v1/studio/sessions/{sess_id}",
        json={"is_validated": False},
        headers=_auth_headers(user_a),
    )

    assert response.status_code == 200
    assert response.json()["is_validated"] is False
    db_session.refresh(session)
    assert session.is_validated is False


def test_confirm_session_admin_allowed(client, admin_user, user_a, db_session) -> None:
    """An admin can confirm/validate another user's session."""
    sess_id = uuid.uuid4()
    s = StudioSession(uuid=sess_id, user_id=user_a.id, device_id="HK-1", label="walk", is_validated=False)
    db_session.add(s)
    db_session.commit()

    res = client.patch(
        f"/api/v1/studio/sessions/{sess_id}/confirm",
        headers=_auth_headers(admin_user),
    )
    assert res.status_code == 200
    assert res.json()["is_validated"] is True

    db_session.refresh(s)
    assert s.is_validated is True


def test_confirm_session_forbidden_for_other_user(client, user_a, user_b, db_session) -> None:
    """A non-admin user cannot confirm another user's session."""
    sess_id = uuid.uuid4()
    s = StudioSession(uuid=sess_id, user_id=user_a.id, device_id="HK-1", label="walk", is_validated=False)
    db_session.add(s)
    db_session.commit()

    res = client.patch(
        f"/api/v1/studio/sessions/{sess_id}/confirm",
        headers=_auth_headers(user_b),
    )
    assert res.status_code == 403

    db_session.refresh(s)
    assert s.is_validated is False


def test_confirm_session_not_found(client, user_a) -> None:
    """Confirming a non-existent session returns 404."""
    fake_id = uuid.uuid4()
    res = client.patch(
        f"/api/v1/studio/sessions/{fake_id}/confirm",
        headers=_auth_headers(user_a),
    )
    assert res.status_code == 404


def test_list_sessions_filter_by_is_validated(client, user_a, db_session) -> None:
    """Listing sessions supports filtering by is_validated flag."""
    s_unvalid = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk", is_validated=False)
    s_valid = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="run", is_validated=True)
    db_session.add_all([s_unvalid, s_valid])
    db_session.commit()

    # Filter validated only
    res_true = client.get("/api/v1/studio/sessions?is_validated=true", headers=_auth_headers(user_a))
    assert res_true.status_code == 200
    data_true = res_true.json()
    assert data_true["total"] == 1
    assert data_true["items"][0]["uuid"] == str(s_valid.uuid)
    assert data_true["items"][0]["is_validated"] is True

    # Filter unvalidated only
    res_false = client.get("/api/v1/studio/sessions?is_validated=false", headers=_auth_headers(user_a))
    assert res_false.status_code == 200
    data_false = res_false.json()
    assert data_false["total"] == 1
    assert data_false["items"][0]["uuid"] == str(s_unvalid.uuid)
    assert data_false["items"][0]["is_validated"] is False

    # No validation filter returns both
    res_all = client.get("/api/v1/studio/sessions", headers=_auth_headers(user_a))
    assert res_all.status_code == 200
    assert res_all.json()["total"] == 2


def test_patch_session_update_is_validated_via_payload(client, user_a, db_session) -> None:
    """PATCH /api/v1/studio/sessions/{session_id} accepts is_validated in payload."""
    sess_id = uuid.uuid4()
    s = StudioSession(uuid=sess_id, user_id=user_a.id, device_id="HK-1", label="walk", is_validated=False)
    db_session.add(s)
    db_session.commit()

    res = client.patch(
        f"/api/v1/studio/sessions/{sess_id}",
        json={"is_validated": True},
        headers=_auth_headers(user_a),
    )
    assert res.status_code == 200
    assert res.json()["is_validated"] is True

    db_session.refresh(s)
    assert s.is_validated is True


def test_trim_session_updates_duration_and_sample_count(
    client, user_a, mock_telemetry_service, db_session
) -> None:
    sess_id = uuid.uuid4()
    session = StudioSession(
        uuid=sess_id,
        user_id=user_a.id,
        device_id="HK-1",
        label="sit_down",
        sample_count=250,
        duration_sec=5.0,
        is_validated=False,
    )
    db_session.add(session)
    db_session.commit()
    mock_telemetry_service.trim_session_readings.return_value = 100

    response = client.patch(
        f"/api/v1/studio/sessions/{sess_id}/trim",
        json={"start_offset_sec": 1.0, "end_offset_sec": 3.0},
        headers=_auth_headers(user_a),
    )

    assert response.status_code == 200
    assert response.json()["duration_sec"] == 2.0
    assert response.json()["sample_count"] == 100
    mock_telemetry_service.trim_session_readings.assert_called_once_with(
        device_id="HK-1",
        session_id=str(sess_id),
        start_offset_sec=1.0,
        end_offset_sec=3.0,
    )
    db_session.refresh(session)
    assert session.duration_sec == 2.0
    assert session.sample_count == 100


def test_trim_session_rejects_validated_session(
    client, user_a, mock_telemetry_service, db_session
) -> None:
    sess_id = uuid.uuid4()
    session = StudioSession(
        uuid=sess_id,
        user_id=user_a.id,
        device_id="HK-1",
        label="walk",
        duration_sec=5.0,
        is_validated=True,
    )
    db_session.add(session)
    db_session.commit()

    response = client.patch(
        f"/api/v1/studio/sessions/{sess_id}/trim",
        json={"new_duration_sec": 2.0},
        headers=_auth_headers(user_a),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Impossible d'écourter une session déjà confirmée/validée"
    mock_telemetry_service.trim_session_readings.assert_not_called()


@pytest.mark.parametrize("new_duration_sec", [5.0, 6.0])
def test_trim_session_requires_shorter_duration(
    client, user_a, mock_telemetry_service, db_session, new_duration_sec
) -> None:
    sess_id = uuid.uuid4()
    session = StudioSession(
        uuid=sess_id,
        user_id=user_a.id,
        device_id="HK-1",
        label="walk",
        duration_sec=5.0,
        is_validated=False,
    )
    db_session.add(session)
    db_session.commit()

    response = client.patch(
        f"/api/v1/studio/sessions/{sess_id}/trim",
        json={"new_duration_sec": new_duration_sec},
        headers=_auth_headers(user_a),
    )

    assert response.status_code == 400
    mock_telemetry_service.trim_session_readings.assert_not_called()


def test_trim_session_rejects_invalid_duration_payload(client, user_a, db_session) -> None:
    session = StudioSession(
        uuid=uuid.uuid4(),
        user_id=user_a.id,
        device_id="HK-1",
        label="walk",
        duration_sec=5.0,
        is_validated=False,
    )
    db_session.add(session)
    db_session.commit()

    response = client.patch(
        f"/api/v1/studio/sessions/{session.uuid}/trim",
        json={"new_duration_sec": 0.5},
        headers=_auth_headers(user_a),
    )

    assert response.status_code == 422


def test_trim_session_rejects_interval_shorter_than_two_seconds(client, user_a, db_session) -> None:
    session = StudioSession(
        uuid=uuid.uuid4(),
        user_id=user_a.id,
        device_id="HK-1",
        label="walk",
        duration_sec=5.0,
        is_validated=False,
    )
    db_session.add(session)
    db_session.commit()

    response = client.patch(
        f"/api/v1/studio/sessions/{session.uuid}/trim",
        json={"start_offset_sec": 2.0, "end_offset_sec": 3.9},
        headers=_auth_headers(user_a),
    )

    assert response.status_code == 422


def test_trim_session_forbidden_for_other_user(
    client, user_a, user_b, mock_telemetry_service, db_session
) -> None:
    session = StudioSession(
        uuid=uuid.uuid4(),
        user_id=user_a.id,
        device_id="HK-1",
        label="walk",
        duration_sec=5.0,
        is_validated=False,
    )
    db_session.add(session)
    db_session.commit()

    response = client.patch(
        f"/api/v1/studio/sessions/{session.uuid}/trim",
        json={"new_duration_sec": 2.0},
        headers=_auth_headers(user_b),
    )

    assert response.status_code == 403
    mock_telemetry_service.trim_session_readings.assert_not_called()


def test_get_session_detail_returns_aurora_sample_count(client, user_a, db_session) -> None:
    """GET /api/v1/studio/sessions/{session_id} directly returns Aurora metadata and sample_count."""
    sess_id = uuid.uuid4()
    s = StudioSession(
        uuid=sess_id,
        user_id=user_a.id,
        device_id="HK-1",
        label="walk",
        sample_count=94,
        duration_sec=5.0,
        is_validated=True,
    )
    db_session.add(s)
    db_session.commit()

    res = client.get(f"/api/v1/studio/sessions/{sess_id}", headers=_auth_headers(user_a))
    assert res.status_code == 200
    data = res.json()
    assert data["uuid"] == str(sess_id)
    assert data["sample_count"] == 94
    assert data["label"] == "walk"
    assert data["is_validated"] is True


def test_get_session_detail_forbidden_for_other_user(client, user_a, user_b, db_session) -> None:
    """GET /api/v1/studio/sessions/{session_id} returns 403 for unauthorized users."""
    sess_id = uuid.uuid4()
    s = StudioSession(
        uuid=sess_id,
        user_id=user_a.id,
        device_id="HK-1",
        label="walk",
        sample_count=94,
    )
    db_session.add(s)
    db_session.commit()

    res = client.get(f"/api/v1/studio/sessions/{sess_id}", headers=_auth_headers(user_b))
    assert res.status_code == 403


def test_regular_user_forbidden_on_all_studio_endpoints(client, regular_user, user_a, db_session) -> None:
    """Regular users without clinician/admin role receive 403 Forbidden across studio session endpoints."""
    sess_id = uuid.uuid4()
    s = StudioSession(uuid=sess_id, user_id=user_a.id, device_id="HK-1", label="walk", sample_count=50)
    db_session.add(s)
    db_session.commit()

    headers = _auth_headers(regular_user)
    assert client.get("/api/v1/studio/sessions", headers=headers).status_code == 403
    assert client.get("/api/v1/studio/sessions/authors", headers=headers).status_code == 403
    assert client.get(f"/api/v1/studio/sessions/{sess_id}", headers=headers).status_code == 403
    assert client.get(f"/api/v1/studio/sessions/{sess_id}/readings", headers=headers).status_code == 403
    assert client.patch(f"/api/v1/studio/sessions/{sess_id}", json={"label": "run"}, headers=headers).status_code == 403
    assert client.patch(f"/api/v1/studio/sessions/{sess_id}/confirm", headers=headers).status_code == 403
    assert client.delete(f"/api/v1/studio/sessions/{sess_id}", headers=headers).status_code == 403


def test_list_sessions_multi_device_filtering(client, user_a, db_session) -> None:
    s1 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk")
    s2 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-2", label="run")
    s3 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-3", label="stairs")
    db_session.add_all([s1, s2, s3])
    db_session.commit()

    # Multi-device "HK-1,HK-2"
    res = client.get("/api/v1/studio/sessions?device_id=HK-1,HK-2", headers=_auth_headers(user_a))
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 2
    dev_ids = {item["device_id"] for item in data["items"]}
    assert dev_ids == {"HK-1", "HK-2"}

    # device_id="all" returns all devices
    res_all = client.get("/api/v1/studio/sessions?device_id=all", headers=_auth_headers(user_a))
    assert res_all.status_code == 200
    assert res_all.json()["total"] == 3


def test_list_sessions_multi_label_filtering(client, user_a, db_session) -> None:
    s1 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk")
    s2 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="run")
    s3 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="stairs")
    db_session.add_all([s1, s2, s3])
    db_session.commit()

    # Multi-label "walk,run"
    res = client.get("/api/v1/studio/sessions?label=walk,run", headers=_auth_headers(user_a))
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 2
    labels = {item["label"] for item in data["items"]}
    assert labels == {"walk", "run"}

    # label="all" returns all sessions
    res_all = client.get("/api/v1/studio/sessions?label=all", headers=_auth_headers(user_a))
    assert res_all.status_code == 200
    assert res_all.json()["total"] == 3


def test_list_sessions_multi_author_filtering(client, admin_user, user_a, user_b, db_session) -> None:
    user_c = User(
        google_sub="sub-user-c",
        email="charlie@example.com",
        name="Charlie",
        role=UserRole.clinician,
        is_active=True,
    )
    db_session.add(user_c)
    db_session.commit()

    s_a = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk")
    s_b = StudioSession(uuid=uuid.uuid4(), user_id=user_b.id, device_id="HK-2", label="run")
    s_c = StudioSession(uuid=uuid.uuid4(), user_id=user_c.id, device_id="HK-3", label="stairs")
    db_session.add_all([s_a, s_b, s_c])
    db_session.commit()

    # Admin filtering by pseudonym: user_a
    res = client.get(
        f"/api/v1/studio/sessions?pseudonym={user_a.pseudonym}",
        headers=_auth_headers(admin_user),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["items"][0]["user_id"] == user_a.id

    # Admin filtering by pseudonym="all" returns all
    res_all = client.get("/api/v1/studio/sessions?pseudonym=all", headers=_auth_headers(admin_user))
    assert res_all.status_code == 200
    assert res_all.json()["total"] == 3

    # Clinician user_a attempting pseudonym filter is still restricted to user_a
    res_clinician = client.get(
        f"/api/v1/studio/sessions?pseudonym={user_b.pseudonym}",
        headers=_auth_headers(user_a),
    )
    assert res_clinician.status_code == 200
    assert res_clinician.json()["total"] == 1
    assert res_clinician.json()["items"][0]["user_id"] == user_a.id


def test_list_studio_authors_admin(client, admin_user, user_a, user_b, db_session) -> None:
    s1 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="walk")
    s2 = StudioSession(uuid=uuid.uuid4(), user_id=user_a.id, device_id="HK-1", label="run")
    s3 = StudioSession(uuid=uuid.uuid4(), user_id=user_b.id, device_id="HK-2", label="stairs")
    db_session.add_all([s1, s2, s3])
    db_session.commit()

    res = client.get("/api/v1/studio/sessions/authors", headers=_auth_headers(admin_user))
    assert res.status_code == 200
    authors = res.json()
    assert len(authors) == 2

    # User A is a third party to admin -> pseudonymized
    assert "id" not in authors[0]
    assert authors[0]["author_uuid"] == str(user_a.uuid)
    assert authors[0]["author_pseudonym"] == user_a.pseudonym
    assert authors[0]["is_pseudonymized"] is True
    assert authors[0]["subject_pseudonym"] == user_a.pseudonym
    assert authors[0]["email"] == f"{authors[0]['subject_pseudonym'].lower()}@pseudonym.healthkicks.local"
    assert authors[0]["name"] == f"Patient {authors[0]['subject_pseudonym']}"

    # User B is a third party to admin -> pseudonymized
    assert "id" not in authors[1]
    assert authors[1]["author_uuid"] == str(user_b.uuid)
    assert authors[1]["author_pseudonym"] == user_b.pseudonym
    assert authors[1]["is_pseudonymized"] is True
    assert authors[1]["subject_pseudonym"] == user_b.pseudonym
    assert authors[1]["email"] == f"{authors[1]['subject_pseudonym'].lower()}@pseudonym.healthkicks.local"
    assert authors[1]["name"] == f"Patient {authors[1]['subject_pseudonym']}"


def test_list_studio_authors_clinician(client, user_a, user_b, db_session) -> None:
    s = StudioSession(uuid=uuid.uuid4(), user_id=user_b.id, device_id="HK-2", label="stairs")
    db_session.add(s)
    db_session.commit()

    res = client.get("/api/v1/studio/sessions/authors", headers=_auth_headers(user_a))
    assert res.status_code == 200
    authors = res.json()
    assert len(authors) == 1
    # Clinician sees themselves with non-pseudonymized personal profile
    assert "id" not in authors[0]
    assert authors[0]["author_uuid"] == str(user_a.uuid)
    assert authors[0]["author_pseudonym"] == user_a.pseudonym
    assert authors[0]["email"] == user_a.email
    assert authors[0]["name"] == user_a.name
    assert authors[0]["is_pseudonymized"] is False
    assert authors[0]["subject_pseudonym"] == user_a.pseudonym


def test_list_studio_authors_rbac_denied(client, regular_user) -> None:
    # 401 unauthenticated
    assert client.get("/api/v1/studio/sessions/authors").status_code == 401

    # 403 regular user
    assert client.get("/api/v1/studio/sessions/authors", headers=_auth_headers(regular_user)).status_code == 403

def test_list_sessions_filter_by_is_hitl(client, user_a, db_session) -> None:
    """Verify that ?is_hitl filter differentiates HITL and standard sessions."""
    s_standard = StudioSession(
        uuid=uuid.uuid4(),
        user_id=user_a.id,
        device_id="HK-1",
        label="walk",
        sample_count=100,
        is_hitl=False,
    )
    s_hitl = StudioSession(
        uuid=uuid.uuid4(),
        user_id=user_a.id,
        device_id="HK-1",
        label="sit_down",
        sample_count=76,
        is_hitl=True,
    )
    db_session.add_all([s_standard, s_hitl])
    db_session.commit()

    # Filter is_hitl=true
    res_hitl = client.get("/api/v1/studio/sessions?is_hitl=true", headers=_auth_headers(user_a))
    assert res_hitl.status_code == 200
    items_hitl = res_hitl.json()["items"]
    assert len(items_hitl) == 1
    assert items_hitl[0]["uuid"] == str(s_hitl.uuid)
    assert items_hitl[0]["is_hitl"] is True

    # Filter is_hitl=false
    res_std = client.get("/api/v1/studio/sessions?is_hitl=false", headers=_auth_headers(user_a))
    assert res_std.status_code == 200
    items_std = res_std.json()["items"]
    assert len(items_std) == 1
    assert items_std[0]["uuid"] == str(s_standard.uuid)
    assert items_std[0]["is_hitl"] is False

    # No is_hitl filter -> returns both
    res_all = client.get("/api/v1/studio/sessions", headers=_auth_headers(user_a))
    assert res_all.status_code == 200
    assert len(res_all.json()["items"]) == 2

