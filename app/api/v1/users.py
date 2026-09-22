"""User management routes."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, RequireAdmin
from app.db.database import get_db
from app.db.models import User, UserRole
from app.schemas.user import UserUpdate


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
    def list_users(admin: RequireAdmin, db: Session = Depends(get_db)) -> list[dict]:
        """List all users (administrator only)."""
        return [_serialize(user) for user in db.query(User).order_by(User.id).all()]

    @router.patch("/{user_id}")
    def update_user(
        user_id: int,
        update: UserUpdate,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> dict:
        """Update user profile. Users can only update their own profile; admins can update anyone."""
        if user.role != UserRole.admin and user.id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: cannot modify another user",
            )

        target_user = db.query(User).filter_by(id=user_id).one_or_none()
        if target_user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        original_email = target_user.email

        if user.role != UserRole.admin:
            if update.role is not None or update.is_active is not None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Forbidden: only administrators can modify role or active status",
                )

        if update.name is not None:
            target_user.name = update.name
        if update.avatar_url is not None:
            target_user.avatar_url = update.avatar_url

        if user.role == UserRole.admin:
            if update.role is not None:
                target_user.role = update.role
            if update.is_active is not None:
                target_user.is_active = update.is_active

        target_user.email = original_email
        db.commit()
        db.refresh(target_user)
        return _serialize(target_user)

    return router
