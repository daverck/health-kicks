"""Pydantic v2 schemas for fall alerting ingestion, emergency contacts CRUD, and alert history."""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models import (
    FallAlertAction,
    FallAlertCancelReason,
    FallAlertDelivery,
)

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VALID_FALL_EVENT_TYPES = {"fall_forward", "fall_backward", "fall_lateral", "fall_generic"}


def _validate_and_clean_name(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not (1 <= len(cleaned) <= 100):
        raise ValueError(f"{field_name} must be between 1 and 100 characters after trimming")
    return cleaned


def _validate_and_clean_email(value: str) -> str:
    cleaned = value.strip().lower()
    if not (3 <= len(cleaned) <= 320) or not EMAIL_REGEX.match(cleaned):
        raise ValueError("Invalid email address format")
    return cleaned


class FallAlertLocationPayload(BaseModel):
    """GPS coordinates attached to a fall alert."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    accuracy_m: float | None = Field(default=None, ge=0.0)
    captured_at: int | None = None


class FallAlertIngestPayload(BaseModel):
    """Webhook payload relayed by AWS IoT Core Lambda on fall alert events."""

    model_config = ConfigDict(extra="forbid")

    user_id: int | str
    event_id: str = Field(min_length=1, max_length=64)
    action: FallAlertAction
    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    event_type: str | None = Field(default=None, min_length=1, max_length=64)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    detected_at: int | None = None
    delivery: FallAlertDelivery | None = None
    location: FallAlertLocationPayload | None = None
    cancel_reason: FallAlertCancelReason | None = None
    sent_at: int | None = None

    @model_validator(mode="after")
    def validate_detected_fields(self) -> "FallAlertIngestPayload":
        if self.action == FallAlertAction.DETECTED:
            missing = []
            if not self.device_id:
                missing.append("device_id")
            if not self.event_type:
                missing.append("event_type")
            if self.confidence is None:
                missing.append("confidence")
            if self.detected_at is None:
                missing.append("detected_at")
            if self.delivery is None:
                missing.append("delivery")
            if missing:
                raise ValueError(
                    f"Missing required fields for DETECTED action: {', '.join(missing)}"
                )
            if self.event_type not in VALID_FALL_EVENT_TYPES:
                raise ValueError(
                    f"Invalid event_type '{self.event_type}'. Must be one of {sorted(VALID_FALL_EVENT_TYPES)}"
                )
        return self


class FallAlertIngestResponse(BaseModel):
    """Response returned by the internal fall alert ingestion endpoint."""

    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    alert_id: int | None
    event_id: str
    state: str
    duplicate: bool = False


class AlertContactCreate(BaseModel):
    """Request body for adding an emergency contact."""

    model_config = ConfigDict(extra="forbid")

    first_name: str
    last_name: str
    email: str

    @field_validator("first_name")
    @classmethod
    def clean_first_name(cls, v: str) -> str:
        return _validate_and_clean_name(v, "first_name")

    @field_validator("last_name")
    @classmethod
    def clean_last_name(cls, v: str) -> str:
        return _validate_and_clean_name(v, "last_name")

    @field_validator("email")
    @classmethod
    def clean_email(cls, v: str) -> str:
        return _validate_and_clean_email(v)


class AlertContactUpdate(BaseModel):
    """Request body for updating an emergency contact."""

    model_config = ConfigDict(extra="forbid")

    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None

    @field_validator("first_name")
    @classmethod
    def clean_first_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_and_clean_name(v, "first_name")

    @field_validator("last_name")
    @classmethod
    def clean_last_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_and_clean_name(v, "last_name")

    @field_validator("email")
    @classmethod
    def clean_email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_and_clean_email(v)

    @model_validator(mode="after")
    def check_at_least_one_field(self) -> "AlertContactUpdate":
        if self.first_name is None and self.last_name is None and self.email is None:
            raise ValueError("At least one field (first_name, last_name, email) must be provided")
        return self


class AlertContactsOrderPayload(BaseModel):
    """Request body for reordering emergency contacts priority."""

    model_config = ConfigDict(extra="forbid")

    contact_ids: list[int]


class AlertContactResponse(BaseModel):
    """Serialized emergency contact."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    first_name: str
    last_name: str
    email: str
    priority: int
    last_test_email_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AlertContactTestEmailResponse(BaseModel):
    """Response after sending a test email to an emergency contact."""

    model_config = ConfigDict(extra="forbid")

    status: str = "sent"
    contact_id: int
    sent_at: datetime


class FallAlertAcknowledgedBy(BaseModel):
    """Summary of the emergency contact who acknowledged the alert."""

    model_config = ConfigDict(extra="forbid")

    first_name: str
    last_name: str


class FallAlertLocationResponse(BaseModel):
    """Serialized GPS coordinates in fall alert detail."""

    model_config = ConfigDict(extra="forbid")

    lat: float
    lon: float
    accuracy_m: float | None = None
    captured_at: datetime | None = None


class FallAlertNotificationResponse(BaseModel):
    """Serialized email notification entry in fall alert detail."""

    model_config = ConfigDict(extra="forbid")

    id: int
    rank: int
    contact_first_name: str
    contact_last_name: str
    contact_email: str
    status: str
    sent_at: datetime
    acknowledged_at: datetime | None = None
    error_message: str | None = None


class FallAlertSummary(BaseModel):
    """Summary item for paginated fall alert history."""

    model_config = ConfigDict(extra="forbid")

    id: int
    event_id: str
    user_id: int
    user_display_name: str
    subject_pseudonym: str
    is_pseudonymized: bool
    device_id: str
    event_type: str
    confidence: float
    state: str
    cancel_reason: str | None = None
    delivery: str
    has_location: bool
    detected_at: datetime
    escalation_started_at: datetime | None = None
    closed_at: datetime | None = None
    acknowledged_by: FallAlertAcknowledgedBy | None = None
    notified_contacts_count: int


class FallAlertDetail(FallAlertSummary):
    """Detailed fall alert view including GPS coordinates and notification timeline."""

    model_config = ConfigDict(extra="forbid")

    location: FallAlertLocationResponse | None = None
    notifications: list[FallAlertNotificationResponse] = Field(default_factory=list)


class PaginatedFallAlertsResponse(BaseModel):
    """Paginated response of fall alert summaries."""

    model_config = ConfigDict(extra="forbid")

    items: list[FallAlertSummary]
    total: int
    page: int
    page_size: int


class FallAlertAckPayload(BaseModel):
    """JSON body option for POST /api/v1/fall-alerts/ack."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1)
