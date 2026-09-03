"""Pydantic schemas for device association endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.models import DeviceStatus


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
