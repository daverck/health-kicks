"""Stateless AWS IoT Core publish adapter."""

import json
import logging
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings, settings
from app.schemas.cloud import HapticTrigger

logger = logging.getLogger(__name__)


class AWSIoTPublishService:
    """Publish C2D messages through the AWS IoT data-plane API only."""

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

    def publish_haptic(self, device_id: str, command: HapticTrigger) -> bool:
        """Publish one normalized haptic command without maintaining a connection."""
        topic = self._config.aws_iot_haptic_command_topic.format(device_id=device_id)
        payload = {"intensity": command.intensity, "duration_ms": command.duration_ms}
        try:
            self._client_for_publish().publish(topic=topic, qos=1, payload=json.dumps(payload))
        except (BotoCoreError, ClientError) as error:
            logger.warning("AWS IoT haptic publish failed for %s: %s", device_id, error)
            return False
        except Exception:
            logger.exception("Unexpected AWS IoT haptic publish failure for %s", device_id)
            return False
        return True