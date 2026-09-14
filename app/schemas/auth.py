"""Pydantic contracts for Authentication and AWS STS IoT credentials."""

from datetime import datetime
from pydantic import BaseModel, ConfigDict


class IoTCredentialsRequest(BaseModel):
    """Optional request payload specifying a target device to scope the credentials."""

    device_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class IoTCredentialsResponse(BaseModel):
    """Temporary AWS STS credentials for connecting to AWS IoT Core via WebSockets SigV4."""

    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: datetime
    iot_endpoint: str
    region: str

    model_config = ConfigDict(extra="forbid")
