"""Strict Pydantic schemas for Studio sessions dataset curation."""

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.cloud import StrictModel


class StudioSessionSummary(StrictModel):
    """Metadata summary of a Studio capture session for dataset curation."""

    id: UUID
    device_id: str
    user_id: int
    user_email: str | None = None
    label: str
    sample_count: int
    duration_sec: float
    created_at: datetime


class StudioSessionUpdatePayload(StrictModel):
    """Payload to update the label of a Studio recording."""

    label: str = Field(min_length=1, max_length=64, description="Reclassified activity label")


class PaginatedSessionsResponse(StrictModel):
    """Paginated collection of studio session summaries."""

    items: list[StudioSessionSummary]
    total: int
    page: int
    size: int
