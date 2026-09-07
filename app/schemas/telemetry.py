"""Strict Pydantic schemas for DynamoDB IMU telemetry."""

from datetime import datetime

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
