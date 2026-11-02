"""Unit tests for Studio class-level detailed statistics endpoint."""

from datetime import UTC, datetime
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
from app.db.models import Base, StudioSession, User, UserRole
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
def clinician_user(db_session) -> User:
    user = User(
        google_sub="test-sub-clinician",
        email="clinician@example.com",
        name="Clinician Dr.",
        role=UserRole.clinician,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def other_user(db_session) -> User:
    user = User(
        google_sub="test-sub-other",
        email="other@example.com",
        name="Other Clinician",
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
        name="Administrator",
        role=UserRole.admin,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def test_client(clinician_user, db_session) -> TestClient:
    app = FastAPI()
    app.include_router(create_telemetry_router(service=TelemetryService(table=MagicMock())))

    def override_get_current_user():
        return clinician_user

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_current_user] = override_get_current_user
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_get_studio_class_stats_empty(test_client, clinician_user, db_session):
    """Test getting stats for a class with zero sessions."""
    resp = test_client.get("/api/v1/studio/classes/walk/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["label"] == "walk"
    assert data["total_sessions"] == 0
    assert data["validated_sessions"] == 0
    assert data["pending_sessions"] == 0
    assert data["duration_metrics"]["median_sec"] == 0.0
    assert data["daily_counts"] == []
    # ML metrics should exist from trained model package
    assert "precision" in data["ml_metrics"]
    assert "recall" in data["ml_metrics"]
    assert "f1_score" in data["ml_metrics"]


def test_get_studio_class_stats_aggregated(test_client, clinician_user, other_user, db_session):
    """Test aggregated stats including daily breakdown, validation counts, and duration metrics."""
    # 3 sessions on 2026-10-25 (2 validated, 1 pending)
    # 2 sessions on 2026-10-26 (2 validated)
    s1 = StudioSession(
        user_id=clinician_user.id,
        device_id="HK-1",
        label="walk",
        duration_sec=4.0,
        sample_count=80,
        is_validated=True,
        created_at=datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC),
    )
    s2 = StudioSession(
        user_id=clinician_user.id,
        device_id="HK-1",
        label="walk",
        duration_sec=6.0,
        sample_count=120,
        is_validated=True,
        created_at=datetime(2026, 10, 25, 11, 0, 0, tzinfo=UTC),
    )
    s3 = StudioSession(
        user_id=clinician_user.id,
        device_id="HK-2",
        label="walk",
        duration_sec=5.0,
        sample_count=100,
        is_validated=False,
        created_at=datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC),
    )
    s4 = StudioSession(
        user_id=clinician_user.id,
        device_id="HK-1",
        label="walk",
        duration_sec=5.0,
        sample_count=100,
        is_validated=True,
        created_at=datetime(2026, 10, 26, 14, 0, 0, tzinfo=UTC),
    )
    s5 = StudioSession(
        user_id=clinician_user.id,
        device_id="HK-1",
        label="walk",
        duration_sec=10.0,
        sample_count=200,
        is_validated=True,
        created_at=datetime(2026, 10, 26, 15, 0, 0, tzinfo=UTC),
    )
    # Session for a different class
    s_other_class = StudioSession(
        user_id=clinician_user.id,
        device_id="HK-1",
        label="idle",
        duration_sec=5.0,
        sample_count=100,
        is_validated=True,
        created_at=datetime(2026, 10, 26, 16, 0, 0, tzinfo=UTC),
    )
    # Session for a different clinician (should be filtered out for non-admin)
    s_other_user = StudioSession(
        user_id=other_user.id,
        device_id="HK-1",
        label="walk",
        duration_sec=5.0,
        sample_count=100,
        is_validated=True,
        created_at=datetime(2026, 10, 26, 17, 0, 0, tzinfo=UTC),
    )
    db_session.add_all([s1, s2, s3, s4, s5, s_other_class, s_other_user])
    db_session.commit()

    resp = test_client.get("/api/v1/studio/classes/walk/stats")
    assert resp.status_code == 200
    data = resp.json()

    assert data["label"] == "walk"
    assert data["total_sessions"] == 5
    assert data["validated_sessions"] == 4
    assert data["pending_sessions"] == 1

    # Durations: [4.0, 6.0, 5.0, 5.0, 10.0] -> sorted: 4.0, 5.0, 5.0, 6.0, 10.0
    # median = 5.0, mean = 6.0, min = 4.0, max = 10.0
    dur = data["duration_metrics"]
    assert dur["median_sec"] == 5.0
    assert dur["mean_sec"] == 6.0
    assert dur["min_sec"] == 4.0
    assert dur["max_sec"] == 10.0
    assert dur["std_sec"] > 0

    # Daily breakdown
    assert len(data["daily_counts"]) == 2
    d1 = data["daily_counts"][0]
    assert d1["date"] == "2026-10-25"
    assert d1["count"] == 3
    assert d1["validated_count"] == 2
    assert d1["pending_count"] == 1

    d2 = data["daily_counts"][1]
    assert d2["date"] == "2026-10-26"
    assert d2["count"] == 2
    assert d2["validated_count"] == 2
    assert d2["pending_count"] == 0



def test_get_studio_classes_summary(test_client, db_session, clinician_user, other_user):
    s1 = StudioSession(
        user_id=clinician_user.id,
        device_id="HK-1",
        label="walk",
        duration_sec=4.0,
        sample_count=80,
        is_validated=True,
        created_at=datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC),
    )
    s2 = StudioSession(
        user_id=clinician_user.id,
        device_id="HK-1",
        label="walk",
        duration_sec=6.0,
        sample_count=120,
        is_validated=False,
        created_at=datetime(2026, 10, 26, 11, 0, 0, tzinfo=UTC),
    )
    s3 = StudioSession(
        user_id=clinician_user.id,
        device_id="HK-1",
        label="idle",
        duration_sec=5.0,
        sample_count=100,
        is_validated=True,
        created_at=datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC),
    )
    s_other_user = StudioSession(
        user_id=other_user.id,
        device_id="HK-2",
        label="run",
        duration_sec=8.0,
        sample_count=160,
        is_validated=True,
        created_at=datetime(2026, 10, 26, 14, 0, 0, tzinfo=UTC),
    )
    db_session.add_all([s1, s2, s3, s_other_user])
    db_session.commit()

    resp = test_client.get("/api/v1/studio/classes/summary")
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_sessions"] == 3
    assert data["validated_sessions"] == 2
    assert data["pending_sessions"] == 1
    assert data["total_duration_sec"] == 15.0

    classes = {c["label"]: c for c in data["classes"]}
    assert "walk" in classes
    assert classes["walk"]["total_sessions"] == 2
    assert classes["walk"]["validated_sessions"] == 1
    assert classes["walk"]["pending_sessions"] == 1
    assert classes["walk"]["total_duration_sec"] == 10.0
    assert classes["walk"]["median_duration_sec"] == 5.0
    assert classes["walk"]["latest_session_date"] == "2026-10-26"

    assert "idle" in classes
    assert classes["idle"]["total_sessions"] == 1
    assert classes["idle"]["validated_sessions"] == 1
