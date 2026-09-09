"""Strict Pydantic schemas for DynamoDB IMU telemetry."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.cloud import StrictModel


class ImuReadingResponse(StrictModel):
    """Normalized IMU reading point from DynamoDB."""

    timestamp_epoch_us: int
    timestamp_iso: datetime
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float
    session_id: str | None = None
    label: str | None = None


class StudioSessionReadingsResponse(StrictModel):
    """Collection of IMU readings captured during a Studio session."""

    device_id: str
    session_id: str
    label: str | None = None
    sample_count: int
    readings: list[ImuReadingResponse]


class StudioStartRequest(StrictModel):
    """Request payload to remotely trigger a Studio recording session."""

    label: str = Field(min_length=1, max_length=64, description="Label d'activité (ex: walk, idle, fall_forward)")
    duration_sec: float = Field(default=5.0, ge=1.0, le=30.0)
    pulse_count: int = Field(default=3, ge=1, le=5)
    pulse_duration_ms: int = Field(default=150, ge=50, le=1000)
    pulse_pause_ms: int = Field(default=350, ge=100, le=1000)
    pulse_intensity: int = Field(default=210, ge=50, le=255)


class StudioStartResponse(StrictModel):
    """Response returned upon successful dispatch of a Studio recording command."""

    status: Literal["command_dispatched"] = "command_dispatched"
    device_id: str
    session_id: str
    label: str
    duration_sec: float
    topic: str


class StudioDatasetStatsResponse(StrictModel):
    """Aggregated session counts for Studio dataset training."""

    device_id: str | None = None
    total_sessions: int
    by_label: dict[str, int]

