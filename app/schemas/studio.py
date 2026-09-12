"""Strict Pydantic schemas for Studio sessions dataset curation."""

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import Field

from app.schemas.cloud import StrictModel


class StudioActivityLabel(str, Enum):
    """Standard activity labels for Studio IMU recordings."""

    WALK = "walk"
    IDLE = "idle"
    STAIRS = "stairs"
    RUN = "run"
    STUMBLE_RECOVER = "stumble_recover"
    FALL_FORWARD = "fall_forward"
    FALL_BACKWARD = "fall_backward"
    FALL_LATERAL = "fall_lateral"


class StudioSessionSummary(StrictModel):
    """Metadata summary of a Studio capture session for dataset curation."""

    id: UUID
    device_id: str
    user_id: int
    user_email: str | None = None
    label: str
    sample_count: int = 0
    duration_sec: float = 5.0
    created_at: datetime


class StudioSessionUpdatePayload(StrictModel):
    """Payload to update the label of a Studio recording."""

    label: str = Field(min_length=1, max_length=64, description="Reclassified activity label (e.g. idle, walk, fall_forward)")


class PaginatedSessionsResponse(StrictModel):
    """Paginated collection of studio session summaries."""

    items: list[StudioSessionSummary]
    total: int
    page: int
    size: int
