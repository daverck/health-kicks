"""AWS IoT command dispatch service."""

import json
import logging
from typing import Any

import boto3

from app.core.config import Settings, settings
from app.schemas.telemetry import StudioStartRequest

logger = logging.getLogger(__name__)


class IotCommandService:
    """Service to dispatch C2D commands to edge devices via AWS IoT Core."""

    def __init__(self, client: Any | None = None, config: Settings = settings) -> None:
        self._client = client
        self._config = config

    def _client_for_publish(self) -> Any:
        if self._client is None:
            endpoint_url = f"https://{self._config.aws_iot_endpoint}" if self._config.aws_iot_endpoint else None
            client_kwargs: dict[str, Any] = {"region_name": self._config.aws_region}
            if endpoint_url:
                client_kwargs["endpoint_url"] = endpoint_url
            return boto3.client("iot-data", **client_kwargs)
        return self._client

    def send_studio_start(self, device_id: str, command: StudioStartRequest, session_id: str) -> str:
        """Publish a studio recording start order to AWS IoT Core."""
        topic = self._config.aws_iot_studio_start_topic.format(device_id=device_id)
        payload = {
            "session_id": session_id,
            "label": command.label,
            "duration_sec": command.duration_sec,
            "pulse_count": command.pulse_count,
            "pulse_duration_ms": command.pulse_duration_ms,
            "pulse_pause_ms": command.pulse_pause_ms,
            "pulse_intensity": command.pulse_intensity,
        }
        logger.info(
            "Publishing studio start command to device %s on topic %s (session_id=%s)",
            device_id,
            topic,
            session_id,
        )
        self._client_for_publish().publish(
            topic=topic,
            qos=1,
            payload=json.dumps(payload),
        )
        return topic
