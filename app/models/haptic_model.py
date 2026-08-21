"""Haptic command schemas."""

from pydantic import BaseModel, Field


class HapticCommand(BaseModel):
    """Command sent to the shoe vibration actuator."""

    intensity: int = Field(ge=0, le=255)
    duration_ms: int = Field(default=500, ge=50, le=10_000)
