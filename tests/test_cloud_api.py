"""Focused tests for the stateless Cloud API boundaries."""

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.controllers.cloud_controller import create_cloud_router
from app.models.database import Base
from app.models.cloud_models import HapticTrigger
from app.models.database import HapticLog
from app.services.aws_iot_publish_service import AWSIoTPublishService
from app.models.database import Device, DeviceStatus
from app.services.ingestion_service import ingest_device_status, ingest_fall_event


def test_iot_publish_uses_normalized_payload_without_network() -> None:
    class FakeIoTData:
        def __init__(self) -> None:
            self.kwargs = None

        def publish(self, **kwargs):
            self.kwargs = kwargs

    client = FakeIoTData()
    service = AWSIoTPublishService(client)

    assert service.publish_haptic("shoe-1", HapticTrigger(intensity=80)) is True
    assert client.kwargs["topic"]
    assert '"device_id": "shoe-1"' in client.kwargs["payload"]


def test_ingestion_persists_normalized_fall_event() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    event = ingest_fall_event(
        session,
        {
            "header": {"device_id": "shoe-1", "msg_id": "message-1"},
            "payload": {
                "timestamp_utc": datetime(2026, 1, 1).isoformat(),
                "confidence_score": 0.95,
                "raw_imu_json": {"ax": 1.0},
            },
        },
    )

    assert event.device_id == "shoe-1"
    assert event.confidence_score == 0.95
    assert event.raw_imu_json == {"ax": 1.0}
    assert session.query(Device).one().status == DeviceStatus.online
    assert session.query(type(event)).count() == 1
    assert ingest_fall_event(
        session,
        {"header": {"device_id": "shoe-1", "msg_id": "message-1"}, "payload": {}},
    ) is None
    assert session.query(type(event)).count() == 1
    session.close()


def test_status_ingestion_updates_device_presence() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    device = ingest_device_status(
        session,
        {"header": {"device_id": "shoe-2"}, "payload": {"status": "offline"}},
    )

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
    endpoint = next(
        route.endpoint
        for route in create_cloud_router(FailedPublisher()).routes
        if route.path == "/api/v1/devices/{device_id}/haptic/trigger"
    )

    try:
        endpoint("shoe-3", HapticTrigger(intensity=80), session)
    except Exception as error:
        assert getattr(error, "status_code", None) == 503
    else:
        raise AssertionError("Expected publication failure")

    log = session.query(HapticLog).one()
    assert log.device_id == "shoe-3"
    assert log.triggered_by_user is True
    session.close()