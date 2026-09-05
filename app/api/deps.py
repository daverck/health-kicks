"""FastAPI security dependencies: authentication and role authorization."""

from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User, UserRole
from app.services.token_service import verify_access_token

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Session = Depends(get_db),
) -> User:
    """Resolve the Bearer JWT to an active database user."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = verify_access_token(credentials.credentials)
    except jwt.ExpiredSignatureError as error:
        raise HTTPException(status_code=401, detail="Token expired") from error
    except jwt.PyJWTError as error:
        raise HTTPException(status_code=401, detail="Invalid token") from error

    user = db.query(User).filter_by(id=int(claims["sub"])).one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Unknown or inactive user")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole):
    """Build a dependency enforcing one of the given roles (admins pass everything)."""

    def _checker(user: CurrentUser) -> User:
        if user.role != UserRole.admin and user.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return user

    return _checker


RequireAdmin = Annotated[User, Depends(require_roles(UserRole.admin))]
