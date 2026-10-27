from uuid import UUID
"""Device association and management routes."""

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.api.v1.utils import encode_cursor
from app.db.database import get_db
from app.db.models import User, UserRole
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
        if payload.user_uuid is not None:
            if user.role != UserRole.admin and str(payload.user_uuid) != str(user.uuid):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Forbidden: cannot bind device for another user",
                )
            target_user = None
            try:
                t_uuid = UUID(str(payload.user_uuid))
                target_user = db.query(User).filter(User.uuid == t_uuid).one_or_none()
            except (ValueError, TypeError):
                pass
            if target_user is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Target user not found",
                )
            target_user_id = target_user.id

        return device_service.bind_device(db=db, user_id=target_user_id, payload=payload)

    @router.get("", response_model=list[DeviceResponse])
    def list_devices(
        user: CurrentUser,
        user_uuid: str | None = Query(None, description="Optional user UUID filter for admins"),
        cursor: str | None = Query(None, description="Opaque cursor (timestamp_utc|device_id) for keyset pagination"),
        skip: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
        db: Session = Depends(get_db),
        response: Response = None,  # type: ignore[assignment]
    ) -> list[DeviceResponse]:
        """List devices bound to the authenticated user (or all devices if admin)."""
        fetch_limit = limit + 1
        if user.role == UserRole.admin:
            if user_uuid is not None:
                target_user = None
                try:
                    t_uuid = UUID(str(user_uuid))
                    target_user = db.query(User).filter(User.uuid == t_uuid).one_or_none()
                except (ValueError, TypeError):
                    pass
                if target_user is None:
                    devices = []
                else:
                    devices = device_service.list_user_devices(
                        db=db, user_id=target_user.id, skip=skip, limit=fetch_limit, cursor=cursor
                    )
            else:
                devices = device_service.list_all_devices(
                    db=db, skip=skip, limit=fetch_limit, cursor=cursor
                )
        else:
            devices = device_service.list_user_devices(
                db=db, user_id=user.id, skip=skip, limit=fetch_limit, cursor=cursor
            )

        if len(devices) > limit:
            items = devices[:limit]
            last_item = items[-1]
            last_ts = last_item.bound_at_utc or last_item.created_at
            next_cursor = encode_cursor(last_ts, last_item.device_id)
        else:
            items = devices
            next_cursor = None

        if response is not None and next_cursor:
            response.headers["X-Next-Cursor"] = next_cursor

        return items

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
