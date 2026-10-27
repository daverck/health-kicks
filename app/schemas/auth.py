"""Pydantic contracts for Authentication and AWS STS IoT credentials."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.pseudonymization import get_subject_pseudonym
from app.db.models import User
from app.schemas.cloud import StrictModel


class UserResponse(StrictModel):
    """User profile response."""

    id: int
    uuid: UUID = Field(default_factory=uuid4)
    pseudonym: str = Field(default="")
    email: str
    name: str | None = None
    avatar_url: str | None = None
    role: str
    is_active: bool

    @field_validator("uuid", mode="before")
    @classmethod
    def parse_uuid(cls, value: Any) -> UUID:
        if isinstance(value, str):
            return UUID(value)
        return value

    @model_validator(mode="after")
    def populate_pseudonym(self) -> "UserResponse":
        if not self.pseudonym:
            self.pseudonym = get_subject_pseudonym(str(self.uuid))
        return self

    @classmethod
    def from_user(cls, user: User) -> "UserResponse":
        u_uuid = getattr(user, "uuid", None)
        if u_uuid is None:
            u_uuid = uuid4()
        u_pseudo = getattr(user, "pseudonym", None)
        if not u_pseudo:
            u_pseudo = get_subject_pseudonym(str(u_uuid))
        return cls(
            id=user.id,
            uuid=u_uuid,
            pseudonym=u_pseudo,
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
    user_id: str
    user_uuid: str

    model_config = ConfigDict(extra="forbid")
