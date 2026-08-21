"""Haptic control HTTP endpoint."""

from fastapi import APIRouter, Body, HTTPException, Query
from typing import Protocol

from app.models.haptic_model import HapticCommand


class HapticPublisher(Protocol):
    """Transport boundary required by the haptic controller."""

    def publish_haptic(self, command: HapticCommand) -> bool:
        """Publish a haptic command."""


def create_router(mqtt_client: HapticPublisher) -> APIRouter:
    """Create haptic routes bound to the MQTT adapter."""
    router = APIRouter(prefix="/api/haptic", tags=["Control"])

    @router.post("/trigger")
    def trigger_vibration(
        command: HapticCommand | None = Body(default=None),
        intensity: int | None = Query(default=None, ge=0, le=255),
        duration_ms: int | None = Query(default=None, ge=50, le=10_000),
    ) -> dict[str, int | str]:
        """Send a validated vibration command to the connected shoe."""
        if command is None:
            if intensity is None:
                raise HTTPException(status_code=422, detail="intensity is required")
            command = HapticCommand(
                intensity=intensity,
                duration_ms=duration_ms if duration_ms is not None else 500,
            )
        if not mqtt_client.publish_haptic(command):
            raise HTTPException(status_code=503, detail="MQTT broker unavailable")
        return {
            "status": "command_sent",
            "intensity": command.intensity,
            "duration_ms": command.duration_ms,
        }

    return router
