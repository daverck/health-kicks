"""Resilient Paho MQTT adapter."""

from collections.abc import Callable
import json
import logging
from threading import Lock
from typing import Any

import paho.mqtt.client as mqtt

from app.core.config import Settings
from app.models.haptic_model import HapticCommand
from app.models.telemetry_model import IMUTelemetry

logger = logging.getLogger(__name__)
TelemetryHandler = Callable[[IMUTelemetry], None]


class MQTTClient:
    """Own the Paho client lifecycle and translate MQTT payloads into models."""

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._connected = False
        self._started = False
        self._lock = Lock()
        self._telemetry_handler: TelemetryHandler | None = None
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=config.mqtt_client_id,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def set_telemetry_handler(self, handler: TelemetryHandler) -> None:
        """Register the application callback for valid telemetry packets."""
        self._telemetry_handler = handler

    def start(self) -> None:
        """Start the network loop and attempt a broker connection."""
        if self._started:
            return
        self._started = True
        try:
            self._client.connect_async(
                self._config.mqtt_broker,
                self._config.mqtt_port,
                self._config.mqtt_keepalive,
            )
            self._client.loop_start()
        except Exception:
            self._started = False
            logger.exception("Unable to start MQTT client; API remains offline")

    def stop(self) -> None:
        """Stop the network loop and disconnect cleanly."""
        if not self._started:
            return
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:
            logger.exception("Error while disconnecting MQTT client")
        finally:
            self._started = False
            self._connected = False

    def publish_haptic(self, command: HapticCommand) -> bool:
        """Publish a haptic command, returning whether it was accepted locally."""
        payload = json.dumps(
            {"intensity": command.intensity, "duration": command.duration_ms}
        )
        with self._lock:
            if not self._connected:
                logger.warning("Haptic command dropped: MQTT broker is offline")
                return False
            result = self._client.publish(self._config.mqtt_haptic_topic, payload)
        return result.rc == mqtt.MQTT_ERR_SUCCESS

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: dict[str, Any],
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None = None,
    ) -> None:
        if reason_code.is_failure:
            logger.error("MQTT connection refused: %s", reason_code)
            return
        with self._lock:
            self._connected = True
        client.subscribe(self._config.mqtt_telemetry_topic)
        logger.info("Connected to MQTT broker %s", self._config.mqtt_broker)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        disconnect_flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None = None,
    ) -> None:
        with self._lock:
            self._connected = False
        if reason_code.value != mqtt.MQTT_ERR_SUCCESS:
            logger.warning("MQTT disconnected (%s); Paho will retry", reason_code)

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        try:
            telemetry = IMUTelemetry.model_validate_json(message.payload)
        except Exception:
            logger.warning("Ignoring invalid MQTT telemetry payload", exc_info=True)
            return
        if self._telemetry_handler is not None:
            self._telemetry_handler(telemetry)
