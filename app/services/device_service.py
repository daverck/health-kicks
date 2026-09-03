"""Service layer for device management and user-device association."""

from datetime import datetime, timedelta, timezone
import logging

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Device, DeviceOwnership
from app.schemas.device import DeviceCreate, DeviceResponse

logger = logging.getLogger(__name__)


def bind_device(db: Session, user_id: int, payload: DeviceCreate) -> DeviceResponse:
    """
    Bind a factory-registered device to a user account:
    1. Verify that the device exists in the factory inventory (devices table).
       If absent, raise HTTP 404.
    2. Check existing ownership for this device:
       a. If already bound to the current user, raise HTTP 400.
       b. If bound to another user, check the last activity (connection / telemetry):
          - If inactive (> device_inactivity_days from config, default 30 days),
            automatically unbind the previous owner and bind to the new user.
          - Otherwise, reject with HTTP 400 and log the event.
    3. Update the device nickname if payload.name is provided.
    4. Create the new DeviceOwnership entry and return DeviceResponse.
    """
    device = db.query(Device).filter_by(device_id=payload.device_id).one_or_none()
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )

    existing_ownerships = (
        db.query(DeviceOwnership)
        .filter_by(device_id=payload.device_id)
        .all()
    )

    # Check if already bound to the requesting user
    if any(o.user_id == user_id for o in existing_ownerships):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Device already bound to this user",
        )

    # Check if currently bound to another user
    if existing_ownerships:
        previous_user_ids = [o.user_id for o in existing_ownerships]
        timestamps = [device.last_seen_utc] + [o.bound_at_utc for o in existing_ownerships if o.bound_at_utc]
        valid_timestamps = [t for t in timestamps if t is not None]

        now = datetime.now(timezone.utc)
        if valid_timestamps:
            last_activity = max(
                t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)
                for t in valid_timestamps
            )
        else:
            last_activity = None

        inactivity_threshold = timedelta(days=settings.device_inactivity_days)
        is_inactive = (last_activity is None) or ((now - last_activity) > inactivity_threshold)

        if is_inactive:
            logger.info(
                "Device %s auto-unbound from user(s) %s due to inactivity (> %s days, last activity: %s) and re-bound to user %s",
                payload.device_id,
                previous_user_ids,
                settings.device_inactivity_days,
                last_activity,
                user_id,
            )
            for o in existing_ownerships:
                db.delete(o)
            db.flush()
        else:
            logger.warning(
                "Device binding rejected: device %s is currently bound to user(s) %s with recent activity at %s (threshold: %s days)",
                payload.device_id,
                previous_user_ids,
                last_activity,
                settings.device_inactivity_days,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Device is already owned by another user",
            )

    # Update nickname if provided
    if payload.name is not None:
        device.name = payload.name

    new_ownership = DeviceOwnership(user_id=user_id, device_id=payload.device_id)
    db.add(new_ownership)
    db.commit()
    db.refresh(device)
    db.refresh(new_ownership)

    return DeviceResponse(
        id=device.id,
        device_id=device.device_id,
        name=device.name,
        status=device.status,
        last_seen_utc=device.last_seen_utc,
        created_at=device.created_at,
        bound_at_utc=new_ownership.bound_at_utc,
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
