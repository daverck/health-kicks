"""Focused tests for Cloud persistence and AWS publication."""

import json
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1.cloud import create_cloud_router
from app.db.models import ActivityEvent, Base, Device, DeviceOwnership, DeviceStatus, HapticLog, User, UserRole
from app.schemas.cloud import HapticTrigger
from app.services.aws_iot_service import AWSIoTPublishService
from app.services.ingestion_service import ingest_device_status

TEST_DEVICE_ID = "HK-1"


def test_iot_publish_uses_normalized_payload_without_network() -> None:
    class FakeIoTData:
        def publish(self, **kwargs):
            self.kwargs = kwargs

    client = FakeIoTData()
    command = HapticTrigger(intensity=80, duration_ms=500)
    assert AWSIoTPublishService(client).publish_haptic(TEST_DEVICE_ID, command) is True
    assert client.kwargs["topic"] == f"healthkicks/v1/{TEST_DEVICE_ID}/commands/haptic"
    payload = json.loads(client.kwargs["payload"])
    assert payload == {"intensity": 80, "duration_ms": 500}
    assert "device_id" not in payload


def test_status_ingestion_updates_device_presence() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    device = ingest_device_status(session, {"header": {"device_id": "shoe-2"}, "payload": {"status": "offline"}})
    assert device.device_id == "shoe-2"
    assert device.status == DeviceStatus.offline
    assert device.last_seen_utc is not None
    session.close()


def test_haptic_failure_is_logged() -> None:
    class FailedPublisher:
        def publish_haptic(self, device_id, command):
            return False

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    admin = User(id=1, email="admin@test.com", name="Admin", role=UserRole.admin)
    endpoint = next(route.endpoint for route in create_cloud_router(FailedPublisher()).routes if route.path.endswith("haptic/trigger"))
    try:
        endpoint("shoe-3", HapticTrigger(intensity=80), user=admin, db=session)
    except Exception as error:
        assert getattr(error, "status_code", None) == 503
    else:
        raise AssertionError("Expected publication failure")
    assert session.query(HapticLog).one().device_id == "shoe-3"
    session.close()


def test_haptic_trigger_records_in_haptic_log_only_and_exposes_history() -> None:
    class SuccessfulPublisher:
        def publish_haptic(self, device_id, command):
            return True

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    router = create_cloud_router(SuccessfulPublisher())

    admin = User(id=1, email="admin@test.com", name="Admin", role=UserRole.admin)
    trigger_endpoint = next(
        route.endpoint for route in router.routes if route.path.endswith("haptic/trigger")
    )
    result = trigger_endpoint(
        TEST_DEVICE_ID,
        HapticTrigger(intensity=120, duration_ms=600),
        user=admin,
        db=session,
    )
    assert result["status"] == "command_sent"
    assert result["device_id"] == TEST_DEVICE_ID
    assert result["intensity"] == 120
    assert result["duration_ms"] == 600

    # Verify HapticLog table contains the vibration record
    haptic_log = session.query(HapticLog).filter_by(device_id=TEST_DEVICE_ID).one()
    assert haptic_log.intensity == 120
    assert haptic_log.duration_ms == 600
    assert haptic_log.triggered_at_utc is not None
    assert haptic_log.triggered_by_user is True

    # Verify ActivityEvent table is NOT polluted with vibrations
    activity_events_count = session.query(ActivityEvent).filter_by(device_id=TEST_DEVICE_ID).count()
    assert activity_events_count == 0

    # Verify dedicated list_haptic_history endpoint
    haptic_history_endpoint = next(
        route.endpoint for route in router.routes if route.path.endswith("haptic/history")
    )
    history_page = haptic_history_endpoint(TEST_DEVICE_ID, user=admin, page=1, page_size=10, db=session)
    assert history_page.total == 1
    assert history_page.items[0].device_id == TEST_DEVICE_ID
    assert history_page.items[0].intensity == 120
    assert history_page.items[0].duration_ms == 600

    # Verify list_activities returns only activities (empty here)
    activities_endpoint = next(
        route.endpoint for route in router.routes if route.path.endswith("events/activities")
    )
    activities_page = activities_endpoint(TEST_DEVICE_ID, user=admin, page=1, page_size=10, db=session)
    assert activities_page.total == 0

    session.close()


def test_list_haptic_history_date_filtering() -> None:
    class DummyPublisher:
        def publish_haptic(self, device_id, command):
            return True

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    admin = User(id=1, email="admin@test.com", name="Admin", role=UserRole.admin)
    router = create_cloud_router(DummyPublisher())
    haptic_history_endpoint = next(
        route.endpoint for route in router.routes if route.path.endswith("haptic/history")
    )

    t1 = datetime(2026, 9, 10, 8, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 9, 11, 14, 0, 0, tzinfo=UTC)
    t3 = datetime(2026, 9, 12, 18, 0, 0, tzinfo=UTC)

    for i, t in enumerate([t1, t2, t3]):
        session.add(
            HapticLog(
                device_id=TEST_DEVICE_ID,
                intensity=50 + i * 10,
                duration_ms=200,
                triggered_at_utc=t,
                triggered_by_user=True,
            )
        )
    session.commit()

    # Filter by start_date and end_date (date-only)
    page = haptic_history_endpoint(
        TEST_DEVICE_ID,
        user=admin,
        start_date=t2,
        end_date=t3,
        page=1,
        page_size=10,
        db=session,
    )
    assert page.total == 2
    assert len(page.items) == 2
    assert page.items[0].intensity == 70  # t3 descending
    assert page.items[1].intensity == 60  # t2

    session.close()


def test_list_haptic_history_invalid_date_range_400() -> None:
    class DummyPublisher:
        def publish_haptic(self, device_id, command):
            return True

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    admin = User(id=1, email="admin@test.com", name="Admin", role=UserRole.admin)
    router = create_cloud_router(DummyPublisher())
    haptic_history_endpoint = next(
        route.endpoint for route in router.routes if route.path.endswith("haptic/history")
    )

    with pytest.raises(HTTPException) as exc_info:
        haptic_history_endpoint(
            TEST_DEVICE_ID,
            user=admin,
            start_date=datetime(2026, 9, 15, tzinfo=UTC),
            end_date=datetime(2026, 9, 10, tzinfo=UTC),
            page=1,
            page_size=10,
            db=session,
        )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "start_date must be before or equal to end_date"

    session.close()


def test_haptic_trigger_unowned_device_forbidden() -> None:
    class DummyPublisher:
        def publish_haptic(self, device_id, command):
            return True

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    regular = User(id=2, email="reg@test.com", name="Regular", role=UserRole.user)
    session.add(regular)
    session.commit()

    router = create_cloud_router(DummyPublisher())
    trigger_endpoint = next(
        route.endpoint for route in router.routes if route.path.endswith("haptic/trigger")
    )

    with pytest.raises(HTTPException) as exc_info:
        trigger_endpoint(
            "unowned-device",
            HapticTrigger(intensity=100),
            user=regular,
            db=session,
        )
    assert exc_info.value.status_code == 403
    session.close()


def test_list_activities_multi_device_and_multi_type() -> None:
    class DummyPublisher:
        def publish_haptic(self, device_id, command):
            return True

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    admin = User(id=1, email="admin@test.com", name="Admin", role=UserRole.admin)
    router = create_cloud_router(DummyPublisher())
    activities_endpoint = next(
        route.endpoint for route in router.routes if route.path.endswith("events/activities")
    )

    t = datetime(2026, 9, 10, 8, 0, 0, tzinfo=UTC)
    session.add_all([
        Device(device_id="HK-1"),
        Device(device_id="HK-2"),
        ActivityEvent(device_id="HK-1", event_type="walk", confidence_score=0.9, timestamp_utc=t),
        ActivityEvent(device_id="HK-2", event_type="run", confidence_score=0.85, timestamp_utc=t),
        ActivityEvent(device_id="HK-1", event_type="stairs_up", confidence_score=0.8, timestamp_utc=t),
        ActivityEvent(device_id="HK-2", event_type="fall_forward", confidence_score=0.95, timestamp_utc=t),
    ])
    session.commit()

    # Multi-device query HK-1,HK-2
    res = activities_endpoint("HK-1,HK-2", user=admin, page=1, page_size=10, db=session)
    assert res.total == 4
    devices = {item.device_id for item in res.items}
    assert devices == {"HK-1", "HK-2"}

    # device_id="all" returns all devices for admin
    res_all = activities_endpoint("all", user=admin, page=1, page_size=10, db=session)
    assert res_all.total == 4

    # Multi-activity filtering: walk,run
    res_types = activities_endpoint("HK-1,HK-2", user=admin, event_type="walk,run", page=1, page_size=10, db=session)
    assert res_types.total == 2
    assert {item.event_type for item in res_types.items} == {"walk", "run"}

    # Multi-activity filtering: stairs_up,falls
    res_falls = activities_endpoint("HK-1,HK-2", user=admin, event_type="stairs_up,falls", page=1, page_size=10, db=session)
    assert res_falls.total == 2
    assert {item.event_type for item in res_falls.items} == {"stairs_up", "fall_forward"}

    session.close()


def test_list_haptic_history_multi_device() -> None:
    class DummyPublisher:
        def publish_haptic(self, device_id, command):
            return True

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    admin = User(id=1, email="admin@test.com", name="Admin", role=UserRole.admin)
    router = create_cloud_router(DummyPublisher())
    haptic_history_endpoint = next(
        route.endpoint for route in router.routes if route.path.endswith("haptic/history")
    )

    t = datetime(2026, 9, 10, 8, 0, 0, tzinfo=UTC)
    session.add_all([
        Device(device_id="HK-1"),
        Device(device_id="HK-2"),
        HapticLog(device_id="HK-1", intensity=100, duration_ms=200, triggered_at_utc=t, triggered_by_user=True),
        HapticLog(device_id="HK-2", intensity=150, duration_ms=300, triggered_at_utc=t, triggered_by_user=True),
    ])
    session.commit()

    # Multi-device query
    res = haptic_history_endpoint("HK-1,HK-2", user=admin, page=1, page_size=10, db=session)
    assert res.total == 2
    assert {item.device_id for item in res.items} == {"HK-1", "HK-2"}

    # device_id="all"
    res_all = haptic_history_endpoint("all", user=admin, page=1, page_size=10, db=session)
    assert res_all.total == 2

    session.close()


def test_list_activities_and_haptic_ownership_check() -> None:
    class DummyPublisher:
        def publish_haptic(self, device_id, command):
            return True

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    regular = User(id=2, email="reg@test.com", name="Regular", role=UserRole.user)
    session.add_all([
        regular,
        Device(device_id="HK-1"),
        Device(device_id="HK-unowned"),
        DeviceOwnership(user_id=2, device_id="HK-1"),
    ])
    session.commit()

    router = create_cloud_router(DummyPublisher())
    activities_endpoint = next(
        route.endpoint for route in router.routes if route.path.endswith("events/activities")
    )
    haptic_history_endpoint = next(
        route.endpoint for route in router.routes if route.path.endswith("haptic/history")
    )

    # Regular user querying unowned device alongside owned one raises 403 Forbidden
    with pytest.raises(HTTPException) as exc_activities:
        activities_endpoint("HK-1,HK-unowned", user=regular, page=1, page_size=10, db=session)
    assert exc_activities.value.status_code == 403

    with pytest.raises(HTTPException) as exc_haptic:
        haptic_history_endpoint("HK-1,HK-unowned", user=regular, page=1, page_size=10, db=session)
    assert exc_haptic.value.status_code == 403

    # device_id="all" for regular user only resolves their owned devices without error
    res_all = activities_endpoint("all", user=regular, page=1, page_size=10, db=session)
    assert res_all.total == 0

    session.close()
