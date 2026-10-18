"""Pydantic contracts for Authentication and AWS STS IoT credentials."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import User
from app.schemas.cloud import StrictModel


class UserResponse(StrictModel):
    """User profile response."""

    id: int
    email: str
    name: str | None = None
    avatar_url: str | None = None
    role: str
    is_active: bool

    @classmethod
    def from_user(cls, user: User) -> "UserResponse":
        return cls(
            id=user.id,
            email=user.email,
            name=user.name,
            avatar_url=user.avatar_url,
            role=user.role.value if hasattr(user.role, "value") else str(user.role),
            is_active=user.is_active,
        )


class TokenResponse(StrictModel):
    """JWT Token pair response."""

    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    user: UserResponse

    @classmethod
    def build(cls, user: User, access_token: str, refresh_token: str | None = None) -> "TokenResponse":
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            user=UserResponse.from_user(user),
        )


class RefreshTokenRequest(StrictModel):
    """Token refresh request."""

    refresh_token: str


class OAuthLoginResponse(StrictModel):
    """OAuth login initialization response."""

    authorization_url: str
    state: str


class OAuthCallbackRequest(StrictModel):
    """OAuth callback POST body."""

    code: str
    state: str
    code_verifier: str | None = Field(default=None, min_length=43, max_length=128)


GoogleLoginResponse = OAuthLoginResponse
GoogleCallbackRequest = OAuthCallbackRequest
AzureLoginResponse = OAuthLoginResponse
AzureCallbackRequest = OAuthCallbackRequest


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
