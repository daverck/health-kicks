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
    is_validated: bool = False
    created_at: datetime


class StudioSessionDetail(StudioSessionSummary):
    """Detailed metadata view of a Studio capture session."""

    pass


class StudioSessionUpdatePayload(StrictModel):
    """Payload to update the label or confirmation status of a Studio recording."""

    label: str | None = Field(default=None, min_length=1, max_length=64, description="Reclassified activity label (e.g. idle, walk, fall_forward)")
    is_validated: bool | None = Field(default=None, description="Validation flag for the capture session")


class PaginatedSessionsResponse(StrictModel):
    """Paginated collection of studio session summaries."""

    items: list[StudioSessionSummary]
    total: int
    page: int
    size: int
