"""Strict Pydantic schemas for User updates."""

from typing import Any
from pydantic import BaseModel, ConfigDict, model_validator

from app.db.models import UserRole


class UserUpdate(BaseModel):
    """Schema for updating user attributes.

    Modifying email is strictly forbidden to maintain consistency with the OIDC IdP.
    """

    model_config = ConfigDict(extra="forbid")

    role: UserRole | None = None
    is_active: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_email(cls, values: Any) -> Any:
        if isinstance(values, dict) and "email" in values:
            raise ValueError("Email address cannot be modified")
        return values


ProfileUpdate = UserUpdate
