"""Application-level stateless JWT access token and refresh token service.

Issues and verifies signed JWT access tokens and refresh tokens used for API authentication.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
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
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def issue_refresh_token(user: User) -> str:
    """Sign a stateless refresh token for session renewals."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "type": "refresh",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.refresh_token_expire_days)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_access_token(token: str) -> dict[str, Any]:
    """Verify one of our own access tokens, raising jwt.PyJWTError on failure."""
    claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    token_type = claims.get("type")
    if token_type is not None and token_type != "access":
        raise jwt.InvalidTokenError("Token is not an access token")
    return claims


def verify_refresh_token(token: str) -> int:
    """Verify a refresh token and return the user ID.

    Raises HTTPException(401) on failure.
    """
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token expired",
        ) from error
    except jwt.PyJWTError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        ) from error

    if claims.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is not a refresh token",
        )

    sub = claims.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token subject",
        )

    try:
        return int(sub)
    except (ValueError, TypeError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token subject",
        ) from error

