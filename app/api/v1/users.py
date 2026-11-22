"""User management routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
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

    @router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
    def delete_me(
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> Response:
        """Self-delete authenticated user account with GDPR pseudonymization and anonymization re-keying."""
        user_service.delete_user_account(db=db, target_user=user)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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

    @router.delete("/{user_uuid}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_user(
        user_uuid: UUID,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> Response:
        """Delete user account by UUID (administrator or account owner)."""
        user_service.delete_user_by_uuid(db=db, current_user=user, target_uuid=user_uuid)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
