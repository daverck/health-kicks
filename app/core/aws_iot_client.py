"""AWS IoT Core MQTT adapter using the AWS IoT Device SDK v2."""

from collections.abc import Callable
import json
import logging
from pathlib import Path
from threading import Lock
from typing import Any

from awscrt import mqtt, io
from awscrt.exceptions import AwsCrtError
from awsiot import mqtt_connection_builder

from app.core.config import Settings
from app.models.haptic_model import HapticCommand
from app.models.telemetry_model import IMUTelemetry

logger = logging.getLogger(__name__)
MessageCallback = Callable[[str, bytes], None]
TelemetryHandler = Callable[[IMUTelemetry], None]
HapticHandler = Callable[[HapticCommand], None]

io.init_logging(io.LogLevel.Debug, 'stderr')


class AWSIoTClient:
    """Secure MQTT client for AWS IoT Core with automatic reconnect."""

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._connection: mqtt.Connection | None = None
        self._connected = False
        self._stopping = False
        self._lock = Lock()
        self._telemetry_handler: TelemetryHandler | None = None
        self._haptic_handler: HapticHandler | None = None
        self._subscriptions: list[tuple[str, MessageCallback]] = []

    def set_telemetry_handler(self, handler: TelemetryHandler) -> None:
        """Register the handler for valid IMU telemetry payloads."""
        self._telemetry_handler = handler

    def set_haptic_handler(self, handler: HapticHandler) -> None:
        """Register the local actuator handler for cloud haptic commands."""
        self._haptic_handler = handler

    @property
    def is_connected(self) -> bool:
        """Return whether the MQTT connection is currently usable."""
        with self._lock:
            return self._connected

    def start(self) -> None:
        """Build the mTLS connection and connect to AWS IoT Core."""
        self._validate_tls_configuration()
        self._stopping = False
        logger.info(
            "AWS IoT connect start: endpoint=%s client_id=%s thing=%s timeout=%ss",
            self._config.aws_iot_endpoint,
            self._config.aws_iot_client_id,
            self._config.aws_iot_thing_name,
            self._config.aws_iot_connect_timeout_seconds,
        )
        logger.info(
            "AWS IoT topics: telemetry=%s haptic=%s",
            self._config.aws_iot_telemetry_topic,
            self._config.aws_iot_haptic_command_topic,
        )
        logger.debug(
            "AWS IoT TLS files: cert=%s key=%s ca=%s",
            self._config.aws_iot_cert_path,
            self._config.aws_iot_private_key_path,
            self._config.aws_iot_root_ca_path,
        )
        logger.info("AWS IoT MQTT transport: mqtt_connection_builder (MQTT 3.1.1)")
        self._connection = mqtt_connection_builder.mtls_from_path(
            endpoint=self._config.aws_iot_endpoint,
            cert_filepath=self._config.aws_iot_cert_path,
            pri_key_filepath=self._config.aws_iot_private_key_path,
            ca_filepath=self._config.aws_iot_root_ca_path,
            client_id=self._config.aws_iot_client_id,
            clean_session=False,
            keep_alive_secs=self._config.mqtt_keepalive,
            on_connection_interrupted=self._on_connection_interrupted,
            on_connection_resumed=self._on_connection_resumed,
        )
        try:
            logger.info("AWS IoT MQTT handshake starting")
            self._connection.connect().result(
                timeout=self._config.aws_iot_connect_timeout_seconds
            )
        except AwsCrtError as error:
            logger.error(
                "AWS IoT handshake failed: name=%s code=%s message=%s",
                error.name,
                error.code,
                error.message,
                exc_info=True,
            )
            self.stop()
            raise
        except TimeoutError:
            logger.error(
                "AWS IoT handshake timed out after %ss",
                self._config.aws_iot_connect_timeout_seconds,
                exc_info=True,
            )
            self.stop()
            raise
        except Exception:
            logger.exception("AWS IoT handshake failed unexpectedly")
            self.stop()
            raise
        with self._lock:
            self._connected = True
        logger.info("AWS IoT connected successfully to %s", self._config.aws_iot_endpoint)
        for topic, callback in self._subscriptions:
            self.subscribe(topic, callback)

    def stop(self) -> None:
        """Disconnect cleanly from AWS IoT Core."""
        self._stopping = True
        connection = self._connection
        if connection is None:
            return
        try:
            connection.disconnect().result(
                timeout=self._config.aws_iot_connect_timeout_seconds
            )
            logger.info("AWS IoT disconnected cleanly")
        except Exception:
            logger.exception("Error while disconnecting from AWS IoT Core")
        finally:
            with self._lock:
                self._connected = False
            self._connection = None

    def publish(self, topic: str, payload: str | bytes) -> bool:
        """Publish an MQTT message with QoS at least once."""
        connection = self._connection
        if connection is None or not self._connected:
            logger.warning("AWS IoT publish dropped while disconnected: %s", topic)
            return False
        try:
            publish_result = connection.publish(
                topic=topic,
                payload=payload,
                qos=mqtt.QoS.AT_LEAST_ONCE,
            )
            publish_future = (
                publish_result[0]
                if isinstance(publish_result, tuple)
                else publish_result
            )
            publish_future.result(timeout=self._config.aws_iot_connect_timeout_seconds)
            logger.debug("Published AWS IoT message to %s", topic)
            return True
        except AwsCrtError as error:
            logger.error(
                "AWS IoT publish failed: topic=%s name=%s code=%s message=%s",
                topic,
                error.name,
                error.code,
                error.message,
                exc_info=True,
            )
            return False
        except Exception:
            logger.exception("AWS IoT publish failed for topic %s", topic)
            return False

    def subscribe(self, topic: str, callback: MessageCallback) -> bool:
        """Subscribe to a topic and retain it for reconnect resubscription."""
        if (topic, callback) not in self._subscriptions:
            self._subscriptions.append((topic, callback))
        connection = self._connection
        if connection is None or not self._connected:
            logger.info("AWS IoT subscription queued until connection: %s", topic)
            return False
        try:
            def on_message(topic: str, payload: bytes) -> None:
                callback(topic, payload)

            subscribe_result = connection.subscribe(
                topic=topic,
                qos=mqtt.QoS.AT_LEAST_ONCE,
                callback=on_message,
            )
            subscribe_future = (
                subscribe_result[0]
                if isinstance(subscribe_result, tuple)
                else subscribe_result
            )
            subscribe_future.result(
                timeout=self._config.aws_iot_connect_timeout_seconds
            )
            if not self.is_connected:
                logger.error(
                    "AWS IoT subscription completed while connection is already disconnected: topic=%s",
                    topic,
                )
                return False
            logger.info("Subscribed to AWS IoT topic %s", topic)
            return True
        except AwsCrtError as error:
            logger.error(
                "AWS IoT subscription failed: topic=%s name=%s code=%s message=%s",
                topic,
                error.name,
                error.code,
                error.message,
                exc_info=True,
            )
            return False
        except Exception:
            logger.exception("AWS IoT subscription failed for topic %s", topic)
            return False

    def publish_haptic(self, command: HapticCommand) -> bool:
        """Publish an API or AI haptic command to the cloud device topic."""
        return self.publish(
            self._config.aws_iot_haptic_command_topic,
            json.dumps(command.model_dump()),
        )

    def _validate_tls_configuration(self) -> None:
        """Fail early with a useful error when AWS mTLS settings are incomplete."""
        if not self._config.aws_iot_endpoint:
            raise RuntimeError("AWS_IOT_ENDPOINT is required for AWS IoT Core")
        for label, path_value in (
            ("certificate", self._config.aws_iot_cert_path),
            ("private key", self._config.aws_iot_private_key_path),
            ("root CA", self._config.aws_iot_root_ca_path),
        ):
            path = Path(path_value)
            logger.info(
                "AWS IoT %s: path=%s exists=%s size=%s",
                label,
                path,
                path.is_file(),
                path.stat().st_size if path.is_file() else "n/a",
            )
            if not path.is_file():
                raise FileNotFoundError(f"AWS IoT {label} file not found: {path}")

    def _on_connection_interrupted(self, connection: mqtt.Connection, error: Any, **_: Any) -> None:
        with self._lock:
            self._connected = False
        logger.warning(
            "AWS IoT connection interrupted: stopping=%s error=%s; SDK will retry",
            self._stopping,
            error,
        )

    def _on_connection_resumed(
        self,
        connection: mqtt.Connection,
        return_code: mqtt.ConnectReturnCode,
        session_present: bool,
        **_: Any,
    ) -> None:
        with self._lock:
            self._connected = True
        logger.info(
            "AWS IoT connection resumed (session_present=%s, return_code=%s)",
            session_present,
            return_code,
        )
        if not session_present:
            for topic, callback in self._subscriptions:
                self.subscribe(topic, callback)

    def handle_telemetry_message(self, _: str, payload: bytes) -> None:
        """Decode an AWS telemetry message and forward valid IMU data."""
        try:
            telemetry = IMUTelemetry.model_validate_json(payload)
        except Exception:
            logger.warning("Ignoring invalid AWS IoT telemetry payload", exc_info=True)
            return
        if self._telemetry_handler is not None:
            self._telemetry_handler(telemetry)

    def handle_haptic_message(self, _: str, payload: bytes) -> None:
        """Decode a cloud haptic command and forward it to the local actuator."""
        try:
            command = HapticCommand.model_validate_json(payload)
        except Exception:
            logger.warning("Ignoring invalid AWS IoT haptic command", exc_info=True)
            return
        logger.info("Received AWS IoT C2D haptic command")
        if self._haptic_handler is not None:
            self._haptic_handler(command)
