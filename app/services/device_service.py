"""Service layer for device management and user-device association."""

from datetime import datetime, timedelta, timezone
import logging

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Device, DeviceOwnership, User, UserRole
from app.schemas.device import DeviceCreate, DeviceResponse

logger = logging.getLogger(__name__)


def verify_device_ownership(
    db: Session,
    user: User,
    device_id: str,
    allow_clinician: bool = False,
) -> None:
    """Verify that a user is authorized to access/operate on a device.

    Admins are always authorized. Clinicians are authorized if allow_clinician is True.
    Regular users must have an active DeviceOwnership record for the device.
    """
    if user.role == UserRole.admin:
        return
    if allow_clinician and user.role == UserRole.clinician:
        return

    ownership = (
        db.query(DeviceOwnership)
        .filter_by(user_id=user.id, device_id=device_id)
        .first()
    )
    if ownership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: device not bound to user",
        )


def bind_device(db: Session, user_id: int, payload: DeviceCreate) -> DeviceResponse:
    """Bind a factory-registered device to a user account."""
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


def list_all_devices(
    db: Session,
    skip: int = 0,
    limit: int = 100,
) -> list[DeviceResponse]:
    """Retrieve all registered devices for administrator overview."""
    rows = (
        db.query(Device, DeviceOwnership.bound_at_utc)
        .outerjoin(DeviceOwnership, Device.device_id == DeviceOwnership.device_id)
        .order_by(Device.created_at.desc())
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
            bound_at_utc=bound_at_utc or device.created_at,
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


def unbind_device_admin(db: Session, device_id: str) -> None:
    """Remove all device ownerships for a device as an administrator."""
    ownerships = db.query(DeviceOwnership).filter_by(device_id=device_id).all()
    if not ownerships:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not bound to any user",
        )
    for o in ownerships:
        db.delete(o)
    db.commit()
