"""Pydantic schemas for device association endpoints."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict

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


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError("timestamp must be an ISO-8601 datetime")


Timestamp = Annotated[datetime, BeforeValidator(_parse_timestamp)]


class DevicePresencePayload(StrictModel):
    """Payload sent by AWS Lambda IoT presence lifecycle event handler."""

    device_id: str
    status: str
    timestamp: Timestamp


class DevicePresenceResponse(StrictModel):
    """Response returned after processing device presence update."""

    status: str
    device_id: str
    device_status: DeviceStatus

