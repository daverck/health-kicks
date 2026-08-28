"""Focused tests for Cloud persistence and AWS publication."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.v1.cloud import create_cloud_router
from app.db.models import Base, DeviceStatus, HapticLog
from app.schemas.cloud import HapticTrigger
from app.services.aws_iot_service import AWSIoTPublishService
from app.services.ingestion_service import ingest_device_status


def test_iot_publish_uses_normalized_payload_without_network() -> None:
    class FakeIoTData:
        def publish(self, **kwargs):
            self.kwargs = kwargs

    client = FakeIoTData()
    assert AWSIoTPublishService(client).publish_haptic("shoe-1", HapticTrigger(intensity=80)) is True
    assert '"device_id": "shoe-1"' in client.kwargs["payload"]


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
        endpoint("shoe-3", HapticTrigger(intensity=80), session)
    except Exception as error:
        assert getattr(error, "status_code", None) == 503
    else:
        raise AssertionError("Expected publication failure")
    assert session.query(HapticLog).one().device_id == "shoe-3"
    session.close()