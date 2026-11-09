"""Unit tests for activity_service querying, authorization, and keyset pagination."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import ActivityEvent, Base, Device, DeviceOwnership, HapticLog, User, UserRole
from app.schemas.cloud import HapticTrigger
from app.services.activity_service import (
    dispatch_haptic_command,
    query_activity_events,
    query_haptic_logs,
    resolve_target_device_ids,
    resolve_temporal_isolation_conditions,
)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def sample_data(db_session):
    u_admin = User(google_sub="sub-admin", email="admin@hk.com", role=UserRole.admin)
    u_user = User(google_sub="sub-user", email="user@hk.com", role=UserRole.user)
    db_session.add_all([u_admin, u_user])
    db_session.commit()
    db_session.refresh(u_admin)
    db_session.refresh(u_user)

    d1 = Device(device_id="dev-1")
    d2 = Device(device_id="dev-2")
    db_session.add_all([d1, d2])
    db_session.commit()

    now = datetime.now(UTC)
    db_session.add(DeviceOwnership(user_id=u_user.id, device_id="dev-1", bound_at_utc=now - timedelta(hours=2)))
    db_session.commit()

    # Pre-binding event
    db_session.add(ActivityEvent(
        device_id="dev-1",
        event_type="walk",
        timestamp_utc=now - timedelta(hours=3),
        confidence_score=0.9,
    ))
    # Post-binding events
    db_session.add(ActivityEvent(
        device_id="dev-1",
        event_type="walk",
        timestamp_utc=now - timedelta(minutes=30),
        confidence_score=0.95,
    ))
    db_session.add(ActivityEvent(
        device_id="dev-1",
        event_type="fall_detected",
        timestamp_utc=now - timedelta(minutes=10),
        confidence_score=0.85,
    ))

    # Haptic logs
    db_session.add(HapticLog(
        device_id="dev-1",
        intensity=80,
        duration_ms=400,
        triggered_at_utc=now - timedelta(minutes=5),
    ))
    db_session.commit()

    return {"admin": u_admin, "user": u_user, "now": now}


def test_resolve_target_device_ids(db_session, sample_data) -> None:
    admin = sample_data["admin"]
    user = sample_data["user"]

    # All devices for admin
    all_admin = resolve_target_device_ids("all", user=admin, db=db_session)
    assert set(all_admin) == {"dev-1", "dev-2"}

    # All devices for user only returns owned devices
    all_user = resolve_target_device_ids("all", user=user, db=db_session)
    assert all_user == ["dev-1"]

    # Specific device ID for user
    dev_user = resolve_target_device_ids("dev-1", user=user, db=db_session)
    assert dev_user == ["dev-1"]


def test_query_activity_events_temporal_isolation(db_session, sample_data) -> None:
    user = sample_data["user"]
    admin = sample_data["admin"]

    # Regular user only sees post-binding events (2 events)
    page_user, _ = query_activity_events(db=db_session, user=user, device_id="dev-1")
    assert page_user.total == 2
    assert len(page_user.items) == 2

    # Admin sees all events including pre-binding (3 events)
    page_admin, _ = query_activity_events(db=db_session, user=admin, device_id="dev-1")
    assert page_admin.total == 3


def test_query_activity_events_type_filtering(db_session, sample_data) -> None:
    admin = sample_data["admin"]

    # Filter by walk
    page_walk, _ = query_activity_events(db=db_session, user=admin, device_id="dev-1", event_type="walk")
    assert page_walk.total == 2

    # Filter by falls
    page_falls, _ = query_activity_events(db=db_session, user=admin, device_id="dev-1", event_type="falls")
    assert page_falls.total == 1
    assert page_falls.items[0].event_type == "fall_detected"


def test_query_haptic_logs_service(db_session, sample_data) -> None:
    user = sample_data["user"]

    page_haptic, cursor = query_haptic_logs(db=db_session, user=user, device_id="dev-1")
    assert page_haptic.total == 1
    assert len(page_haptic.items) == 1
    assert page_haptic.items[0].intensity == 80
    assert cursor is None


class DummyPublisher:
    def __init__(self, succeeds: bool = True):
        self.succeeds = succeeds
        self.calls = []

    def publish_haptic(self, device_id: str, command: HapticTrigger) -> bool:
        self.calls.append((device_id, command))
        return self.succeeds


def test_dispatch_haptic_command_service_success(db_session) -> None:
    pub = DummyPublisher(succeeds=True)
    res = dispatch_haptic_command(
        db=db_session,
        publisher=pub,
        device_id="dev-new",
        command=HapticTrigger(intensity=90, duration_ms=500),
    )
    assert res["status"] == "command_sent"
    assert res["device_id"] == "dev-new"
    assert res["intensity"] == 90
    assert res["duration_ms"] == 500

    # Device auto-created
    dev = db_session.query(Device).filter_by(device_id="dev-new").one_or_none()
    assert dev is not None

    # HapticLog persisted
    log = db_session.query(HapticLog).filter_by(device_id="dev-new").first()
    assert log is not None
    assert log.intensity == 90
    assert log.duration_ms == 500
    assert log.triggered_by_user is True


def test_dispatch_haptic_command_service_publish_failed(db_session) -> None:
    from fastapi import HTTPException
    pub = DummyPublisher(succeeds=False)
    with pytest.raises(HTTPException) as exc_info:
        dispatch_haptic_command(
            db=db_session,
            publisher=pub,
            device_id="dev-fail",
            command=HapticTrigger(intensity=50, duration_ms=200),
        )
    assert exc_info.value.status_code == 503
    assert "AWS IoT publish unavailable" in exc_info.value.detail


def test_resolve_temporal_isolation_conditions_admin_and_user(db_session, sample_data) -> None:
    admin = sample_data["admin"]
    user = sample_data["user"]

    # Admin: unrestricted access, no filter clause
    has_access, clause = resolve_temporal_isolation_conditions(
        db=db_session,
        user=admin,
        target_device_ids=["dev-1", "dev-2"],
        device_id_col=ActivityEvent.device_id,
        timestamp_col=ActivityEvent.timestamp_utc,
    )
    assert has_access is True
    assert clause is None

    # User with owned device: access granted with temporal filter clause
    has_access, clause = resolve_temporal_isolation_conditions(
        db=db_session,
        user=user,
        target_device_ids=["dev-1"],
        device_id_col=ActivityEvent.device_id,
        timestamp_col=ActivityEvent.timestamp_utc,
    )
    assert has_access is True
    assert clause is not None

    # User with unowned device: access denied
    has_access, clause = resolve_temporal_isolation_conditions(
        db=db_session,
        user=user,
        target_device_ids=["dev-unowned"],
        device_id_col=ActivityEvent.device_id,
        timestamp_col=ActivityEvent.timestamp_utc,
    )
    assert has_access is False
    assert clause is None

