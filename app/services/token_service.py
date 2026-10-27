"""Application-level stateless JWT access token and refresh token service.

Issues and verifies signed JWT access tokens and refresh tokens used for API authentication.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
import uuid as _uuid

import jwt
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import User


def issue_access_token(user: User) -> str:
    """Sign a stateless access token for API calls."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.uuid),
        "user_id": user.id,
        "google_sub": user.google_sub,
        "azure_sub": user.azure_sub,
        "email": user.email,
        "role": user.role.value if hasattr(user.role, "value") else str(user.role),
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def issue_refresh_token(user: User) -> str:
    """Sign a stateless refresh token for session renewals."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.uuid),
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


def resolve_user_from_sub(db: Session, sub: Any) -> User | None:
    """Resolve a database user from token subject: UUID first, then integer fallback."""
    if not sub:
        return None
    sub_str = str(sub).strip()
    try:
        val_uuid = _uuid.UUID(sub_str)
        user = db.query(User).filter(User.uuid == val_uuid).one_or_none()
        if user is not None:
            return user
    except (ValueError, TypeError, AttributeError):
        pass

    try:
        val_int = int(sub_str)
        return db.query(User).filter(User.id == val_int).one_or_none()
    except (ValueError, TypeError):
        pass

    return None


def verify_refresh_token(token: str) -> str:
    """Verify a refresh token and return the user subject string.

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

    sub_str = str(sub).strip()
    # Subject must be either a valid UUID or an integer
    is_valid = False
    try:
        _uuid.UUID(sub_str)
        is_valid = True
    except (ValueError, TypeError, AttributeError):
        pass

    if not is_valid:
        try:
            int(sub_str)
            is_valid = True
        except (ValueError, TypeError):
            pass

    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token subject",
        )

    return sub_str


# Aliases per specification
create_access_token = issue_access_token
create_refresh_token = issue_refresh_token
decode_token = verify_access_token
