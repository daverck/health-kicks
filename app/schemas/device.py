from uuid import UUID, uuid4
"""Pydantic schemas for device association endpoints."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, model_validator

from app.db.models import DeviceStatus
from app.schemas.cloud import StrictModel


class DeviceCreate(BaseModel):
    """Payload used to bind a device to the authenticated user."""

    device_id: str | None = None
    name: str | None = None
    user_uuid: UUID | str | None = None

    @model_validator(mode="before")
    @classmethod
    def set_default_device_id(cls, data: object) -> object:
        if isinstance(data, dict):
            did = data.get("device_id")
            if did is None or not str(did).strip():
                data["device_id"] = str(uuid4())
        return data

    @model_validator(mode="after")
    def validate_device_id(self) -> "DeviceCreate":
        if not self.device_id or not self.device_id.strip():
            self.device_id = str(uuid4())
        return self


class DeviceResponse(BaseModel):
    """A device bound to a user, including connectivity and hardware health metadata."""

    model_config = ConfigDict(from_attributes=True)

    uuid: UUID
    device_id: str
    name: str | None = None
    status: DeviceStatus
    is_online: bool = False
    last_seen: datetime | None = None
    last_seen_utc: datetime | None = None
    firmware_version: str | None = None
    last_calibration_time: datetime | None = None
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
