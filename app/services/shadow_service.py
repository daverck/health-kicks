"""AWS IoT Device Shadow application service."""

import json
import logging
from threading import Lock
from typing import Any, Callable

from app.core.aws_iot_client import AWSIoTClient
from app.core.config import Settings
from app.models.shadow_model import ShadowDocument, ShadowState

logger = logging.getLogger(__name__)
DesiredStateHandler = Callable[[dict[str, Any]], None]


class ShadowService:
    """Publish, observe, and expose the connected shoe Device Shadow."""

    def __init__(self, client: AWSIoTClient, config: Settings) -> None:
        self._client = client
        self._config = config
        self._state = ShadowDocument()
        self._lock = Lock()
        self._desired_handler: DesiredStateHandler | None = None

    def set_desired_state_handler(self, handler: DesiredStateHandler) -> None:
        """Register business logic applied to desired-state changes."""
        self._desired_handler = handler

    def start(self) -> None:
        """Subscribe to shadow deltas and accepted/rejected updates."""
        self._client.subscribe(self._shadow_update_topic("delta"), self._on_delta)
        self._client.subscribe(self._shadow_update_topic("accepted"), self._on_update)
        self._client.subscribe(self._shadow_update_topic("rejected"), self._on_rejected)
        self._client.subscribe(self._shadow_update_topic("get/accepted"), self._on_get)
        self._client.publish(
            self._config.aws_iot_shadow_get_topic,
            json.dumps({}),
        )
        logger.info("Device Shadow subscriptions initialized")

    def get_state(self) -> ShadowDocument:
        """Return a snapshot of the last known Shadow state."""
        with self._lock:
            return self._state.model_copy(deep=True)

    def update_desired(self, state: ShadowState) -> bool:
        """Publish a desired-state patch to AWS IoT Device Shadow."""
        payload = json.dumps({"state": {"desired": state.as_dict()}})
        published = self._client.publish(self._config.aws_iot_shadow_update_topic, payload)
        if published:
            logger.info("Device Shadow desired state update published")
        return published

    def publish_reported(self, state: dict[str, Any]) -> bool:
        """Publish a reported-state patch to AWS IoT Device Shadow."""
        payload = json.dumps({"state": {"reported": state}})
        published = self._client.publish(self._config.aws_iot_shadow_update_topic, payload)
        if published:
            logger.info("Device Shadow reported state update published")
        return published

    def _shadow_update_topic(self, suffix: str) -> str:
        """Build an official AWS IoT Shadow topic from the configured thing name."""
        return f"$aws/things/{self._config.aws_iot_thing_name}/shadow/update/{suffix}"

    def _on_delta(self, _: str, payload: bytes) -> None:
        try:
            document = json.loads(payload)
            desired = document.get("state", {}).get("desired", {})
            with self._lock:
                self._state.desired.update(desired)
            logger.info("Received Device Shadow desired-state delta: %s", desired)
            if self._desired_handler is not None:
                self._desired_handler(desired)
        except (TypeError, json.JSONDecodeError):
            logger.warning("Ignoring invalid Device Shadow delta", exc_info=True)

    def _on_update(self, _: str, payload: bytes) -> None:
        self._merge_document(payload)
        logger.info("Device Shadow update accepted")

    def _on_get(self, _: str, payload: bytes) -> None:
        self._merge_document(payload)
        logger.info("Device Shadow state received")

    def _on_rejected(self, _: str, payload: bytes) -> None:
        logger.error("Device Shadow update rejected: %s", payload.decode(errors="replace"))

    def _merge_document(self, payload: bytes) -> None:
        try:
            document = json.loads(payload)
            state = document.get("state", {})
            with self._lock:
                self._state = ShadowDocument(
                    desired=state.get("desired", self._state.desired),
                    reported=state.get("reported", self._state.reported),
                    version=document.get("version", self._state.version),
                )
        except (TypeError, json.JSONDecodeError):
            logger.warning("Ignoring invalid Device Shadow response", exc_info=True)
