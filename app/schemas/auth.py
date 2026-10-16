"""Pydantic contracts for Authentication and AWS STS IoT credentials."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.cloud import StrictModel


class OAuthTicketExchangeRequest(StrictModel):
    """Request payload for exchanging a one-time OAuth ticket for session tokens."""

    ticket: str = Field(..., min_length=16, max_length=256)
    code_verifier: str | None = Field(default=None, min_length=43, max_length=128)


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
    user_id: str | None = None

    model_config = ConfigDict(extra="forbid")


