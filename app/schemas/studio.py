"""Strict Pydantic schemas for Studio sessions dataset curation."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas.cloud import StrictModel


class StudioActivityLabel(str, Enum):
    """Standard activity labels for Studio IMU recordings."""

    WALK = "walk"
    IDLE = "idle"
    STAIRS = "stairs"
    STAIRS_UP = "stairs_up"
    STAIRS_DOWN = "stairs_down"
    RUN = "run"
    STUMBLE_RECOVER = "stumble_recover"
    FALL_FORWARD = "fall_forward"
    FALL_BACKWARD = "fall_backward"
    FALL_LATERAL = "fall_lateral"
    FALL_RECOVERY = "fall_recovery"


class StudioSessionSummary(StrictModel):
    """Metadata summary of a Studio capture session for dataset curation."""

    id: UUID
    device_id: str
    user_id: int
    user_email: str | None = None
    author_name: str | None = None
    subject_pseudonym: str | None = None
    is_pseudonymized: bool = False
    label: str
    sample_count: int = 0
    duration_sec: float = 5.0
    is_validated: bool = False
    is_hitl: bool = False
    created_at: datetime


class StudioSessionDetail(StudioSessionSummary):
    """Detailed metadata view of a Studio capture session."""


class StudioSessionTrimPayload(StrictModel):
    """Payload to keep a time interval from an unvalidated Studio recording."""

    start_offset_sec: float = Field(
        default=0.0,
        ge=0.0,
        le=29.5,
        description="Start offset of the retained session interval in seconds",
    )
    end_offset_sec: float | None = Field(
        default=None,
        gt=0.5,
        le=30.0,
        description="End offset of the retained session interval in seconds",
    )
    new_duration_sec: float | None = Field(
        default=None,
        gt=0.5,
        le=30.0,
        json_schema_extra={"deprecated": True},
        description="Legacy end offset for trimming from the beginning",
    )

    @model_validator(mode="after")
    def validate_trim_interval(self) -> "StudioSessionTrimPayload":
        if self.end_offset_sec is None and self.new_duration_sec is None:
            raise ValueError("An end_offset_sec is required")
        if self.end_offset_sec is not None and self.new_duration_sec is not None:
            raise ValueError("Use end_offset_sec instead of new_duration_sec")
        end_offset_sec = self.end_offset_sec or self.new_duration_sec
        if end_offset_sec is None or end_offset_sec - self.start_offset_sec < 2.0:
            raise ValueError("The retained interval must be at least 2 seconds")
        return self



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
    next_cursor: str | None = Field(default=None, description="Opaque cursor for keyset pagination of next page")
    prev_cursor: str | None = Field(default=None, description="Opaque cursor for keyset pagination of previous page")


class StudioAuthorSummary(StrictModel):
    """Author metadata summary for studio recording curation."""

    id: int = Field(..., description="Unique user identifier")
    email: str = Field(..., description="User email address")
    name: str | None = Field(default=None, description="User full display name")
    subject_pseudonym: str | None = Field(default=None, description="Deterministic pseudonymized subject identifier (GDPR)")
    is_pseudonymized: bool = Field(default=False, description="True if author PII has been pseudonymized for privacy")


class StudioSessionDatasetRecord(StrictModel):
    """Normalized schema for cached training sessions (DynamoDB/NPZ)."""

    session_id: str
    device_id: str = "HK-1"
    label: str
    duration_sec: float = 0.0
    sample_count: int = 0
    source_duration_sec: float | None = None
    source_sample_count: int | None = None
    readings: list[dict[str, Any]] = Field(default_factory=list)
