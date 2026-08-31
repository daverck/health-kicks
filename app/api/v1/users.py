"""User management routes (admin only)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import RequireAdmin
from app.db.database import get_db
from app.db.models import User, UserRole


class UserUpdate(BaseModel):
    role: UserRole | None = None
    is_active: bool | None = None


def _serialize(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "role": user.role.value,
        "is_active": user.is_active,
        "created_at": user.created_at,
        "last_login_utc": user.last_login_utc,
    }


def create_users_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/users", tags=["Users"])

    @router.get("")
    def list_users(admin: User = Depends(RequireAdmin), db: Session = Depends(get_db)) -> list[dict]:
        return [_serialize(user) for user in db.query(User).order_by(User.id).all()]

    @router.patch("/{user_id}")
    def update_user(
        user_id: int,
        update: UserUpdate,
        admin: User = Depends(RequireAdmin),
        db: Session = Depends(get_db),
    ) -> dict:
        user = db.query(User).filter_by(id=user_id).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        if update.role is not None:
            user.role = update.role
        if update.is_active is not None:
            user.is_active = update.is_active
        db.commit()
        db.refresh(user)
        return _serialize(user)

    return router
