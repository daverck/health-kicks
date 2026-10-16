"""Strict AWS IoT Rule webhook contracts."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BeforeValidator, Field

from app.schemas.cloud import StrictModel


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError("timestamp must be an ISO-8601 datetime")


Timestamp = Annotated[datetime, BeforeValidator(_parse_timestamp)]


class IngestionHeader(StrictModel):
    device_id: str = Field(min_length=1, max_length=128)
    msg_id: str = Field(min_length=1, max_length=255)
    timestamp_utc: Timestamp = Field(validation_alias=AliasChoices("timestamp", "timestamp_utc"))


class IngestionPayload(StrictModel):
    event_type: str = Field(min_length=1, max_length=64)
    confidence_score: float = Field(ge=0, le=1)
    raw_imu_snapshot: dict[str, float]


class DeviceStatusHeader(StrictModel):
    device_id: str = Field(min_length=1, max_length=128)
    msg_id: str | None = Field(default=None, min_length=1, max_length=255)
    timestamp_utc: Timestamp | None = Field(
        default=None,
        validation_alias=AliasChoices("timestamp", "timestamp_utc"),
    )


class IngestionEvent(StrictModel):
    header: IngestionHeader
    payload: IngestionPayload


class DeviceStatusPayload(StrictModel):
    status: Literal["online", "offline"]


class DeviceStatusEvent(StrictModel):
    header: DeviceStatusHeader
    payload: DeviceStatusPayload


class IngestionResponse(StrictModel):
    status: str
    msg_id: str
    duplicate: bool


class RawTelemetryIngestionRequest(StrictModel):
    device_id: str | None = Field(default=None, max_length=128)
    session_id: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default="unlabeled", max_length=64)
    sample_count: int | None = Field(default=0, ge=0)
    duration_sec: float | None = Field(default=5.0, ge=0)
    readings: list[dict[str, Any]] | None = Field(default_factory=list)
