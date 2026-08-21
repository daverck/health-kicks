"""Tests for AWS IoT message boundaries without a network connection."""

import json
from threading import Event
import time

import pytest
from awscrt.exceptions import AwsCrtError

from app.core.aws_iot_client import AWSIoTClient
from app.core.config import settings
from app.models.haptic_model import HapticCommand
from app.models.shadow_model import ShadowState
from app.services.shadow_service import ShadowService


@pytest.mark.integration
def test_real_aws_connection_healthkicks_topics() -> None:
    """
    Publish and receive telemetry through AWS IoT Core with mTLS.
    """

    received = Event()
    received_payload: list[bytes] = []
    client = AWSIoTClient(settings)

    def on_message(_: str, payload: bytes) -> None:
        received_payload.append(payload)
        received.set()

    assert settings.aws_iot_client_id == "healthkicks-backend"
    assert settings.aws_iot_telemetry_topic.startswith("healthkicks/")
    assert settings.aws_iot_haptic_command_topic.startswith("healthkicks/")

    try:
        client.start()
    except AwsCrtError as error:
        pytest.fail(
            "AWS IoT rejected the healthkicks-backend connection. Verify that "
            "the certificate used by this test is attached to a policy allowing "
            "iot:Connect on client/healthkicks-backend: "
            f"{error}"
        )
    try:
        assert client.is_connected
        assert client.subscribe(settings.aws_iot_telemetry_topic, on_message)

        payload = json.dumps(
            {
                "ax": 0.01,
                "ay": 0.02,
                "az": 1.0,
                "gx": 0.0,
                "gy": 0.0,
                "gz": 0.0,
                "timestamp": time.time(),
            }
        )
        assert client.publish(settings.aws_iot_telemetry_topic, payload)
        assert received.wait(timeout=10), "Telemetry was not received from AWS IoT"
        assert json.loads(received_payload[-1]) == json.loads(payload)
    except Exception as e:
        pytest.fail(
            "AWS IoT publish/subscribe failed."
            f"{e}")

    finally:
        client.stop()

    assert client.is_connected is False


class FakeAWSClient:
    """Capture AWS IoT publications and subscriptions without a network."""

    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.published: list[tuple[str, str | bytes]] = []
        self.subscriptions = []

    def publish(self, topic: str, payload: str | bytes) -> bool:
        self.published.append((topic, payload))
        return self.accepted

    def subscribe(self, topic: str, callback) -> bool:
        self.subscriptions.append((topic, callback))
        return True


def test_shadow_updates_desired_state_on_expected_topic() -> None:
    client = FakeAWSClient()
    service = ShadowService(client, settings)

    assert service.update_desired(ShadowState(vibration_enabled=True)) is True

    topic, payload = client.published[0]
    assert topic == settings.aws_iot_shadow_update_topic
    assert json.loads(payload) == {"state": {"desired": {"vibration_enabled": True}}}


def test_shadow_delta_is_stored_and_forwarded() -> None:
    client = AWSIoTClient(settings)
    desired_states = []
    service = ShadowService(client, settings)
    service.set_desired_state_handler(desired_states.append)
    service.start()

    service._on_delta("delta", b'{"state":{"desired":{"sensibility_level":75}}}')

    assert service.get_state().desired == {"sensibility_level": 75}
    assert desired_states == [{"sensibility_level": 75}]


def test_c2d_haptic_payload_reaches_local_handler() -> None:
    client = AWSIoTClient(settings)
    received: list[HapticCommand] = []
    client.set_haptic_handler(received.append)

    client.handle_haptic_message(
        settings.aws_iot_haptic_command_topic,
        b'{"intensity":200,"duration_ms":300}',
    )

    assert received == [HapticCommand(intensity=200, duration_ms=300)]
