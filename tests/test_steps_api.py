"""Tests for daily activity steps synchronization and history endpoints."""

from datetime import date, timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import get_db
from app.db.models import Base, DailyActivityStep, User, UserRole
from app.main import app
from app.services import token_service


@pytest.fixture()
def db_session():
    """In-memory SQLite session for isolated database tests."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def client(db_session):
    """Test client with database session override."""
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def regular_user(db_session) -> User:
    """Create a regular test user."""
    user = User(
        google_sub="sub-regular-user",
        email="regular@example.com",
        name="Regular User",
        role=UserRole.user,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def other_user(db_session) -> User:
    """Create a secondary test user for isolation testing."""
    user = User(
        google_sub="sub-other-user",
        email="other@example.com",
        name="Other User",
        role=UserRole.user,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def admin_user(db_session) -> User:
    """Create an administrator test user."""
    user = User(
        google_sub="sub-admin-user",
        email="admin@example.com",
        name="Admin User",
        role=UserRole.admin,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def auth_headers_regular(regular_user) -> dict[str, str]:
    token = token_service.issue_access_token(regular_user)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def auth_headers_other(other_user) -> dict[str, str]:
    token = token_service.issue_access_token(other_user)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def auth_headers_admin(admin_user) -> dict[str, str]:
    token = token_service.issue_access_token(admin_user)
    return {"Authorization": f"Bearer {token}"}


def test_sync_steps_nominal(client, auth_headers_regular, db_session) -> None:
    """Test standard multi-activity sync payload ingestion."""
    payload = {
        "device_id": "HK-SHOE-001",
        "date": "2026-09-22",
        "activities": [
            {"activity_type": "walk", "step_count": 4120},
            {"activity_type": "run", "step_count": 1850},
            {"activity_type": "stairs", "step_count": 310},
            {"activity_type": "unclassified", "step_count": 45},
        ],
    }

    res = client.post("/api/v1/steps/sync", json=payload, headers=auth_headers_regular)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "synchronized"
    assert data["synced_records"] == 4

    # Verify rows in DB
    records = db_session.query(DailyActivityStep).filter_by(device_id="HK-SHOE-001").all()
    assert len(records) == 4
    counts = {r.activity_type: r.step_count for r in records}
    assert counts == {"walk": 4120, "run": 1850, "stairs": 310, "unclassified": 45}


def test_sync_steps_idempotency_and_update(client, auth_headers_regular, db_session) -> None:
    """Test that submitting repeated snapshots updates step count rather than inserting duplicates."""
    initial_payload = {
        "device_id": "HK-SHOE-001",
        "date": "2026-09-22",
        "activities": [
            {"activity_type": "walk", "step_count": 1000},
            {"activity_type": "run", "step_count": 500},
        ],
    }
    res1 = client.post("/api/v1/steps/sync", json=initial_payload, headers=auth_headers_regular)
    assert res1.status_code == 200

    # Submit updated counts later in the day
    updated_payload = {
        "device_id": "HK-SHOE-001",
        "date": "2026-09-22",
        "activities": [
            {"activity_type": "walk", "step_count": 2500},
            {"activity_type": "run", "step_count": 800},
            {"activity_type": "stairs", "step_count": 150},
        ],
    }
    res2 = client.post("/api/v1/steps/sync", json=updated_payload, headers=auth_headers_regular)
    assert res2.status_code == 200
    assert res2.json()["synced_records"] == 3

    records = db_session.query(DailyActivityStep).filter_by(device_id="HK-SHOE-001").all()
    assert len(records) == 3
    counts = {r.activity_type: r.step_count for r in records}
    assert counts == {"walk": 2500, "run": 800, "stairs": 150}


def test_get_steps_history_dynamic_aggregation(client, auth_headers_regular) -> None:
    """Test retrieval and dynamic total computation across dates."""
    # Day 1
    client.post(
        "/api/v1/steps/sync",
        json={
            "device_id": "HK-SHOE-001",
            "date": "2026-09-20",
            "activities": [
                {"activity_type": "walk", "step_count": 3000},
                {"activity_type": "run", "step_count": 2000},
            ],
        },
        headers=auth_headers_regular,
    )

    # Day 2
    client.post(
        "/api/v1/steps/sync",
        json={
            "device_id": "HK-SHOE-001",
            "date": "2026-09-21",
            "activities": [
                {"activity_type": "walk", "step_count": 4500},
                {"activity_type": "stairs", "step_count": 500},
            ],
        },
        headers=auth_headers_regular,
    )

    res = client.get(
        "/api/v1/steps/history?device_id=HK-SHOE-001&from_date=2026-09-20&to_date=2026-09-21",
        headers=auth_headers_regular,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["device_id"] == "HK-SHOE-001"
    assert data["from_date"] == "2026-09-20"
    assert data["to_date"] == "2026-09-21"
    assert len(data["history"]) == 2

    day1 = data["history"][0]
    assert day1["date"] == "2026-09-20"
    assert day1["total_steps"] == 5000
    assert day1["by_activity"] == {"walk": 3000, "run": 2000}

    day2 = data["history"][1]
    assert day2["date"] == "2026-09-21"
    assert day2["total_steps"] == 5000
    assert day2["by_activity"] == {"stairs": 500, "walk": 4500}


def test_get_steps_history_invalid_range(client, auth_headers_regular) -> None:
    """Test 422 error when from_date is later than to_date."""
    res = client.get(
        "/api/v1/steps/history?device_id=HK-SHOE-001&from_date=2026-09-25&to_date=2026-09-20",
        headers=auth_headers_regular,
    )
    assert res.status_code == 422
    assert "from_date cannot be greater than to_date" in res.json()["detail"]


def test_steps_unauthenticated_access(client) -> None:
    """Test 401 unauthorized when accessing steps endpoints without token."""
    res_sync = client.post("/api/v1/steps/sync", json={"device_id": "HK-1", "date": "2026-09-22", "activities": []})
    assert res_sync.status_code == 401

    res_hist = client.get("/api/v1/steps/history?device_id=HK-1")
    assert res_hist.status_code == 401


def test_steps_rbac_isolation(client, auth_headers_regular, auth_headers_other, auth_headers_admin) -> None:
    """Test user isolation for steps history: normal user only sees their own uploads; admin sees all."""
    client.post(
        "/api/v1/steps/sync",
        json={
            "device_id": "HK-SHARED-DEV",
            "date": "2026-09-22",
            "activities": [{"activity_type": "walk", "step_count": 5000}],
        },
        headers=auth_headers_regular,
    )

    # Other user queries the same device: should receive empty history
    res_other = client.get(
        "/api/v1/steps/history?device_id=HK-SHARED-DEV&from_date=2026-09-22&to_date=2026-09-22",
        headers=auth_headers_other,
    )
    assert res_other.status_code == 200
    assert len(res_other.json()["history"]) == 0

    # Admin queries the device: should see the data
    res_admin = client.get(
        "/api/v1/steps/history?device_id=HK-SHARED-DEV&from_date=2026-09-22&to_date=2026-09-22",
        headers=auth_headers_admin,
    )
    assert res_admin.status_code == 200
    assert len(res_admin.json()["history"]) == 1
    assert res_admin.json()["history"][0]["total_steps"] == 5000

