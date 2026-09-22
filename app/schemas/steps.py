"""Pydantic schemas for daily activity step counting and synchronization."""

from datetime import date as date_type
from pydantic import BaseModel, ConfigDict, Field


class ActivityStepItem(BaseModel):
    """Step count payload for a specific activity type."""

    activity_type: str = Field(
        ...,
        description="Activity type name (e.g., 'walk', 'run', 'stairs', 'unclassified')",
        min_length=1,
        max_length=32,
    )
    step_count: int = Field(
        ...,
        description="Non-negative accumulated step count for this activity",
        ge=0,
    )

    model_config = ConfigDict(from_attributes=True)


class DailyStepsSyncPayload(BaseModel):
    """Payload to synchronize daily step counts from an edge device/mobile app."""

    device_id: str = Field(
        ...,
        description="Unique identifier of the IoT footwear device",
        min_length=1,
        max_length=64,
    )
    date: date_type = Field(
        ...,
        description="Calendar date of the recorded steps (YYYY-MM-DD)",
    )
    activities: list[ActivityStepItem] = Field(
        ...,
        description="List of step counts broken down by activity type",
    )

    model_config = ConfigDict(from_attributes=True)


class DailyStepsSyncResponse(BaseModel):
    """Response returned upon successful step synchronization."""

    status: str = Field(
        default="synchronized",
        description="Synchronization status indicator",
    )
    synced_records: int = Field(
        ...,
        description="Number of activity step records upserted",
    )


class DailyStepsSummary(BaseModel):
    """Daily step summary including dynamic total and per-activity breakdown."""

    date: date_type = Field(..., description="Calendar date (YYYY-MM-DD)")
    total_steps: int = Field(..., description="Dynamically computed sum of steps across all activities")
    by_activity: dict[str, int] = Field(
        default_factory=dict,
        description="Mapping of activity types to their corresponding step counts",
    )

    model_config = ConfigDict(from_attributes=True)


class DailyStepsHistoryResponse(BaseModel):
    """Historical steps response across a date range."""

    device_id: str = Field(..., description="Target device identifier")
    from_date: date_type = Field(..., description="Beginning of the query window (inclusive)")
    to_date: date_type = Field(..., description="End of the query window (inclusive)")
    history: list[DailyStepsSummary] = Field(
        default_factory=list,
        description="Chronological list of daily step summaries",
    )

    model_config = ConfigDict(from_attributes=True)

