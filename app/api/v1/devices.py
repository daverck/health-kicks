"""Device association and management routes."""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.db.database import get_db
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
        return device_service.bind_device(db=db, user_id=user.id, payload=payload)

    @router.get("", response_model=list[DeviceResponse])
    def list_devices(
        user: CurrentUser,
        skip: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
        db: Session = Depends(get_db),
    ) -> list[DeviceResponse]:
        """List all devices bound to the authenticated user with pagination."""
        return device_service.list_user_devices(db=db, user_id=user.id, skip=skip, limit=limit)

    @router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
    def unbind_device(
        device_id: str,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> None:
        """Dissociate/unbind a device from the authenticated user."""
        device_service.unbind_device(db=db, user_id=user.id, device_id=device_id)

    return router

