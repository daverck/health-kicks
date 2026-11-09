"""User management routes."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, RequireAdmin
from app.db.database import get_db
from app.schemas.user import UserUpdate
from app.services import user_service


def create_users_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/users", tags=["Users"])

    @router.get("")
    def list_users(admin: RequireAdmin, db: Session = Depends(get_db)) -> list[dict]:
        """List all users (administrator only)."""
        return user_service.list_users(db=db)

    @router.patch("/{user_uuid}")
    def update_user(
        user_uuid: UUID,
        update: UserUpdate,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> dict:
        """Update user profile. Users can only update their own profile; admins can update anyone."""
        return user_service.update_user_profile(
            db=db,
            current_user=user,
            user_uuid=user_uuid,
            update=update,
        )

    return router
