"""Tests for API schemas, route contracts, and MQTT boundary behavior."""

from fastapi import HTTPException

from app.controllers.haptic_controller import create_router
from app.core.config import settings
from app.core.mqtt_client import MQTTClient
from app.main import app
from app.models.haptic_model import HapticCommand


class FakeMQTTClient:
    """Capture haptic commands without connecting to a broker."""

    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.commands = []

    def publish_haptic(self, command: HapticCommand) -> bool:
        self.commands.append(command)
        return self.accepted


def get_trigger_endpoint(fake_client: FakeMQTTClient):
    """Return the endpoint function from the controller router."""
    router = create_router(fake_client)
    return next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/haptic/trigger"
    )


def test_openapi_exposes_expected_endpoints() -> None:
    paths = app.openapi()["paths"]

    assert "/api/telemetry/latest" in paths
    assert "/api/telemetry/history" in paths
    assert "/api/haptic/trigger" in paths


def test_haptic_controller_accepts_json_command() -> None:
    fake_client = FakeMQTTClient()
    endpoint = get_trigger_endpoint(fake_client)

    response = endpoint(command=HapticCommand(intensity=100, duration_ms=250))

    assert response == {
        "status": "command_sent",
        "intensity": 100,
        "duration_ms": 250,
    }
    assert fake_client.commands == [HapticCommand(intensity=100, duration_ms=250)]


def test_haptic_controller_preserves_query_parameter_contract() -> None:
    fake_client = FakeMQTTClient()
    endpoint = get_trigger_endpoint(fake_client)

    response = endpoint(command=None, intensity=80, duration_ms=300)

    assert response["status"] == "command_sent"
    assert fake_client.commands == [HapticCommand(intensity=80, duration_ms=300)]


def test_haptic_controller_reports_mqtt_outage() -> None:
    endpoint = get_trigger_endpoint(FakeMQTTClient(accepted=False))

    try:
        endpoint(command=None, intensity=80, duration_ms=None)
    except HTTPException as error:
        assert error.status_code == 503
    else:
        raise AssertionError("Expected a 503 when MQTT publication is unavailable")


def test_mqtt_client_drops_haptic_command_when_offline() -> None:
    client = MQTTClient(settings)

    assert client.publish_haptic(HapticCommand(intensity=80)) is False
