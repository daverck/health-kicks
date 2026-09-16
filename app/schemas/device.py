"""Pydantic schemas for device association endpoints."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, model_validator

from app.db.models import DeviceStatus
from app.schemas.cloud import StrictModel


class DeviceCreate(BaseModel):
    """Payload used to bind a device to the authenticated user."""

    device_id: str
    name: str | None = None


class DeviceResponse(BaseModel):
    """A device bound to a user, including the binding timestamp."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: str
    name: str | None = None
    status: DeviceStatus
    last_seen_utc: datetime | None = None
    created_at: datetime
    bound_at_utc: datetime


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError("timestamp must be an ISO-8601 datetime")


Timestamp = Annotated[datetime | None, BeforeValidator(_parse_timestamp)]


class DevicePresencePayload(StrictModel):
    """Payload sent by AWS Lambda IoT presence lifecycle event handler."""

    device_id: str | None = None
    user_id: int | str | None = None
    status: str | None = None
    state: str | None = None
    timestamp: Timestamp = None

    @property
    def effective_state(self) -> str:
        val = self.state or self.status or "offline"
        return val.lower().strip()

    @model_validator(mode="after")
    def validate_payload(self) -> "DevicePresencePayload":
        if not self.device_id and self.user_id is None:
            raise ValueError("Either device_id or user_id must be provided")
        if not self.status and not self.state:
            raise ValueError("Either state or status must be provided")
        return self


class DevicePresenceResponse(StrictModel):
    """Response returned after processing device presence update."""

    status: str
    device_id: str | None = None
    device_status: DeviceStatus | None = None

