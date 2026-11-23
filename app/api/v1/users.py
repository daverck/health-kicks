"""User management routes."""

from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, RequireAdmin
from app.db.database import get_db
from app.schemas.audit import AuditAction, AuditResource
from app.schemas.user import UserUpdate
from app.services import audit_service, export_service, user_service


def create_users_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/users", tags=["Users"])

    @router.post("/me/export", response_model=None)
    def export_my_data(
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> Response:
        """GDPR Article 20 Right to Data Portability: export all user data in a structured ZIP archive."""
        zip_bytes, filename = export_service.generate_export_zip(db=db, user=user)
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": "application/zip",
            },
        )

    @router.get("")
    def list_users(
        admin: RequireAdmin,
        request: Request,
        db: Session = Depends(get_db),
    ) -> list[dict]:
        """List all users (administrator only)."""
        users = user_service.list_users(db=db)
        return users

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
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict:
        """Update user profile. Users can only update their own profile; admins can update anyone."""
        result = user_service.update_user_profile(
            db=db,
            current_user=user,
            user_uuid=user_uuid,
            update=update,
        )
        if user.uuid != user_uuid:
            audit_service.record_access_log(
                db=db,
                actor=user,
                patient=user_uuid,
                action=AuditAction.UPDATE,
                resource=AuditResource.profile,
                patient_pseudonym=result.get("pseudonym"),
                request=request,
            )
        return result

    @router.delete("/{user_uuid}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_user(
        user_uuid: UUID,
        user: CurrentUser,
        request: Request,
        db: Session = Depends(get_db),
    ) -> Response:
        """Delete user account by UUID (administrator or account owner)."""
        if user.uuid != user_uuid:
            audit_service.record_access_log(
                db=db,
                actor=user,
                patient=user_uuid,
                action=AuditAction.DELETE,
                resource=AuditResource.profile,
                request=request,
            )
        user_service.delete_user_by_uuid(db=db, current_user=user, target_uuid=user_uuid)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
