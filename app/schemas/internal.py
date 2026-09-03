"""Internal API schemas (e.g. for AWS IoT presence webhook from Lambda)."""

from app.schemas.device import DevicePresencePayload, DevicePresenceResponse

__all__ = ["DevicePresencePayload", "DevicePresenceResponse"]
