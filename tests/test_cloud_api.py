"""Focused tests for Cloud persistence and AWS publication."""

import json
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1.cloud import create_cloud_router
from app.db.models import Base, DeviceStatus, FallEvent, FallStatus, HapticLog
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
    endpoint = next(route.endpoint for route in create_cloud_router(FailedPublisher()).routes if route.path.endswith("haptic/trigger"))
    try:
        endpoint("shoe-3", HapticTrigger(intensity=80), user=None, db=session)
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

    trigger_endpoint = next(
        route.endpoint for route in router.routes if route.path.endswith("haptic/trigger")
    )
    result = trigger_endpoint(
        TEST_DEVICE_ID,
        HapticTrigger(intensity=120, duration_ms=600),
        user=None,
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

    # Verify FallEvent table is NOT polluted with vibrations
    fall_events_count = session.query(FallEvent).filter_by(device_id=TEST_DEVICE_ID).count()
    assert fall_events_count == 0

    # Verify dedicated list_haptic_history endpoint
    haptic_history_endpoint = next(
        route.endpoint for route in router.routes if route.path.endswith("haptic/history")
    )
    history_page = haptic_history_endpoint(TEST_DEVICE_ID, user=None, page=1, page_size=10, db=session)
    assert history_page.total == 1
    assert history_page.items[0].device_id == TEST_DEVICE_ID
    assert history_page.items[0].intensity == 120
    assert history_page.items[0].duration_ms == 600

    # Verify list_falls returns only falls (empty here)
    falls_endpoint = next(
        route.endpoint for route in router.routes if route.path.endswith("events/falls")
    )
    falls_page = falls_endpoint(TEST_DEVICE_ID, user=None, page=1, page_size=10, db=session)
    assert falls_page.total == 0

    session.close()