"""Application-level stateless JWT access token service.

Issues and verifies signed JWT access tokens used for API authentication.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.core.config import settings
from app.db.models import User


def issue_access_token(user: User) -> str:
    """Sign a stateless access token for API calls."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "google_sub": user.google_sub,
        "azure_sub": user.azure_sub,
        "email": user.email,
        "role": user.role.value,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_access_token(token: str) -> dict[str, Any]:
    """Verify one of our own access tokens, raising jwt.PyJWTError on failure."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
