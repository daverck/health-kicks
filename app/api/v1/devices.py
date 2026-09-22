"""Device association and management routes."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.db.database import get_db
from app.db.models import UserRole
from app.schemas.device import DeviceCreate, DeviceResponse
from app.services import device_service


def create_devices_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/devices", tags=["Devices"])

    @router.post("", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED)
    def bind_device(
        payload: DeviceCreate,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> DeviceResponse:
        """Bind a device to the authenticated user account."""
        target_user_id = user.id
        if payload.user_id is not None and payload.user_id != user.id:
            if user.role != UserRole.admin:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Forbidden: cannot bind device for another user",
                )
            target_user_id = payload.user_id

        return device_service.bind_device(db=db, user_id=target_user_id, payload=payload)

    @router.get("", response_model=list[DeviceResponse])
    def list_devices(
        user: CurrentUser,
        user_id: int | None = Query(None, description="Optional user ID filter for admins"),
        skip: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
        db: Session = Depends(get_db),
    ) -> list[DeviceResponse]:
        """List devices bound to the authenticated user (or all devices if admin)."""
        if user.role == UserRole.admin:
            if user_id is not None:
                return device_service.list_user_devices(db=db, user_id=user_id, skip=skip, limit=limit)
            return device_service.list_all_devices(db=db, skip=skip, limit=limit)
        return device_service.list_user_devices(db=db, user_id=user.id, skip=skip, limit=limit)

    @router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
    def unbind_device(
        device_id: str,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> None:
        """Dissociate/unbind a device."""
        if user.role == UserRole.admin:
            device_service.unbind_device_admin(db=db, device_id=device_id)
        else:
            device_service.unbind_device(db=db, user_id=user.id, device_id=device_id)

    return router
