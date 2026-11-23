"""Pydantic schemas for GDPR Article 20 data portability export."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ExportMetadata(BaseModel):
    """Metadata describing the data portability archive."""

    model_config = ConfigDict(extra="forbid")

    export_timestamp_utc: datetime = Field(
        description="ISO-8601 UTC timestamp when export was generated"
    )
    schema_version: str = Field(
        default="1.0.0", description="Data portability export schema version"
    )
    user_uuid: UUID = Field(description="Internal user UUID")
    user_pseudonym: str = Field(description="Public pseudonym SUBJ-XXXXXXXX")
    gdpr_article: str = Field(
        default="Article 20 - Right to data portability",
        description="Legal basis under European Regulation (EU) 2016/679",
    )
    item_counts: dict[str, int] = Field(
        default_factory=dict, description="Count of exported items across categories"
    )


class UserProfileExport(BaseModel):
    """Demographic and account profile data."""

    model_config = ConfigDict(extra="forbid")

    uuid: UUID
    pseudonym: str
    email: str
    name: str | None
    avatar_url: str | None
    role: str
    is_active: bool
    created_at: datetime
    last_login_utc: datetime | None


class AlertContactExport(BaseModel):
    """Configured emergency contact."""

    model_config = ConfigDict(extra="forbid")

    uuid: UUID
    first_name: str
    last_name: str
    email: str
    phone: str | None
    priority: int
    last_test_email_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DeviceExport(BaseModel):
    """Paired footwear or gateway IoT hardware."""

    model_config = ConfigDict(extra="forbid")

    device_id: str
    name: str | None
    status: str
    firmware_version: str | None
    bound_at_utc: datetime
    last_seen_utc: datetime | None


class FallNotificationExport(BaseModel):
    """Delivery log for emergency contact alert escalation."""

    model_config = ConfigDict(extra="forbid")

    notification_uuid: UUID
    contact_email: str | None
    contact_phone: str | None
    status: str
    sent_at_utc: datetime | None
    acknowledged_at_utc: datetime | None
    error_message: str | None


class FallAlertExport(BaseModel):
    """Fall alert incident record."""

    model_config = ConfigDict(extra="forbid")

    uuid: UUID
    event_id: str
    state: str
    event_type: str
    confidence: float | None
    delivery: str
    location: dict[str, Any] | None
    detected_at_utc: datetime
    resolved_at_utc: datetime | None
    cancel_reason: str | None
    notifications: list[FallNotificationExport] = Field(default_factory=list)


class FallAlertsSummary(BaseModel):
    """Summary of fall alert incidents."""

    model_config = ConfigDict(extra="forbid")

    total_alerts: int
    by_state: dict[str, int] = Field(default_factory=dict)
    active_years: list[int] = Field(default_factory=list)


class HourlyStepExport(BaseModel):
    """Hourly step count for a specific hour and activity."""

    model_config = ConfigDict(extra="forbid")

    hour: int
    activity_type: str
    step_count: int
    device_id: str
    updated_at: datetime


class DailyStepSummary(BaseModel):
    """Aggregated daily step counts with hourly breakdown."""

    model_config = ConfigDict(extra="forbid")

    date: date
    device_id: str
    activity_type: str
    total_steps: int
    updated_at: datetime
    hourly_breakdown: list[HourlyStepExport] = Field(default_factory=list)


class MonthlyStepsExport(BaseModel):
    """Consolidated step data for an active month."""

    model_config = ConfigDict(extra="forbid")

    month: str = Field(description="YYYY-MM format")
    total_steps: int
    days_recorded: int
    by_activity: dict[str, int] = Field(default_factory=dict)
    daily_records: list[DailyStepSummary] = Field(default_factory=list)


class ActivityStepsSummary(BaseModel):
    """Lifetime summary of activity step metrics."""

    model_config = ConfigDict(extra="forbid")

    total_steps: int
    active_months: list[str] = Field(default_factory=list)
    by_activity: dict[str, int] = Field(default_factory=dict)


class StudioSessionSummaryExport(BaseModel):
    """Brief manifest item for studio recording sessions."""

    model_config = ConfigDict(extra="forbid")

    uuid: UUID
    device_id: str
    label: str
    sample_count: int
    duration_sec: float
    is_validated: bool
    is_hitl: bool
    created_at: datetime


class StudioSessionDetailExport(BaseModel):
    """Detailed export of an individual studio session."""

    model_config = ConfigDict(extra="forbid")

    uuid: UUID
    device_id: str
    label: str
    sample_count: int
    duration_sec: float
    is_validated: bool
    is_hitl: bool
    created_at: datetime
