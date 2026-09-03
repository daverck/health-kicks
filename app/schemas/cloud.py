"""Strict Cloud API contracts."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class DeviceResponse(StrictModel):
    id: int
    device_id: str
    name: str | None = None
    status: str
    last_seen_utc: datetime | None = None
    created_at: datetime


class HapticTrigger(StrictModel):
    intensity: int = Field(ge=0, le=255)
    duration_ms: int = Field(default=500, ge=50, le=10_000)


class FallEventResponse(StrictModel):
    id: int
    device_id: str
    event_type: str = "fall"
    timestamp_utc: datetime
    confidence_score: float | None = None
    raw_imu_json: dict
    status_enum: str

    model_config = ConfigDict(strict=True, extra="forbid", from_attributes=True)


class FallEventPage(StrictModel):
    items: list[FallEventResponse]
    page: int
    page_size: int
    total: int


class HealthResponse(StrictModel):
    status: str
    database: str


class HapticLogResponse(StrictModel):
    id: int
    device_id: str
    intensity: int
    duration_ms: int
    triggered_at_utc: datetime
    triggered_by_user: bool

    model_config = ConfigDict(strict=True, extra="forbid", from_attributes=True)


class HapticLogPage(StrictModel):
    items: list[HapticLogResponse]
    page: int
    page_size: int
    total: int