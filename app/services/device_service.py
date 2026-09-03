"""Service layer for device management and user-device association."""

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.db.models import Device, DeviceOwnership
from app.schemas.device import DeviceCreate, DeviceResponse


def bind_device(db: Session, user_id: int, payload: DeviceCreate) -> DeviceResponse:
    """
    Bind a device to a user:
    1. Retrieve or create the device in the devices table.
    2. If device already exists and a name is provided, update its name.
    3. Check if (user_id, device_id) already exists in device_ownership.
    4. If already bound, raise HTTP 400.
    5. Otherwise, create DeviceOwnership record and return DeviceResponse.
    """
    device = db.query(Device).filter_by(device_id=payload.device_id).one_or_none()
    if device is None:
        device = Device(device_id=payload.device_id, name=payload.name)
        db.add(device)
        db.flush()
    elif payload.name is not None:
        device.name = payload.name

    existing_ownership = (
        db.query(DeviceOwnership)
        .filter_by(user_id=user_id, device_id=payload.device_id)
        .one_or_none()
    )
    if existing_ownership is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Device already bound to this user",
        )

    ownership = DeviceOwnership(user_id=user_id, device_id=payload.device_id)
    db.add(ownership)
    db.commit()
    db.refresh(device)
    db.refresh(ownership)

    return DeviceResponse(
        id=device.id,
        device_id=device.device_id,
        name=device.name,
        status=device.status,
        last_seen_utc=device.last_seen_utc,
        created_at=device.created_at,
        bound_at_utc=ownership.bound_at_utc,
    )


def list_user_devices(
    db: Session,
    user_id: int,
    skip: int = 0,
    limit: int = 100,
) -> list[DeviceResponse]:
    """Retrieve all devices bound to a user, joined with ownership for bound_at_utc."""
    rows = (
        db.query(Device, DeviceOwnership.bound_at_utc)
        .join(DeviceOwnership, Device.device_id == DeviceOwnership.device_id)
        .filter(DeviceOwnership.user_id == user_id)
        .order_by(DeviceOwnership.bound_at_utc.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [
        DeviceResponse(
            id=device.id,
            device_id=device.device_id,
            name=device.name,
            status=device.status,
            last_seen_utc=device.last_seen_utc,
            created_at=device.created_at,
            bound_at_utc=bound_at_utc,
        )
        for device, bound_at_utc in rows
    ]


def unbind_device(db: Session, user_id: int, device_id: str) -> None:
    """Remove device ownership for the given user and device."""
    ownership = (
        db.query(DeviceOwnership)
        .filter_by(user_id=user_id, device_id=device_id)
        .one_or_none()
    )
    if ownership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not bound to this user",
        )
    db.delete(ownership)
    db.commit()
