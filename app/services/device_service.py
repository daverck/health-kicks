"""Service layer for device management and user-device association."""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.api.v1.utils import decode_cursor, encode_cursor
from app.core.config import settings
from app.db.models import Device, DeviceOwnership, DeviceStatus, User, UserRole
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


def resolve_target_user_for_binding(
    db: Session,
    current_user: User,
    target_user_uuid: UUID | str | None,
) -> int:
    """Resolve the target user ID for a device binding request.

    If target_user_uuid is None, returns current_user.id.
    Non-admin users can only bind devices to themselves.
    Raises HTTPException(403) or HTTPException(404) if invalid.
    """
    if target_user_uuid is None:
        return current_user.id

    if current_user.role != UserRole.admin and str(target_user_uuid) != str(current_user.uuid):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: cannot bind device for another user",
        )

    try:
        t_uuid = UUID(str(target_user_uuid))
        target_user = db.query(User).filter(User.uuid == t_uuid).one_or_none()
    except (ValueError, TypeError):
        target_user = None

    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target user not found",
        )

    return target_user.id


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

        now = datetime.now(UTC)
        if valid_timestamps:
            last_activity = max(
                t if t.tzinfo is not None else t.replace(tzinfo=UTC)
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
        uuid=device.uuid,
        device_id=device.device_id,
        name=device.name,
        status=device.status,
        is_online=(device.status == DeviceStatus.online),
        last_seen=device.last_seen_utc,
        last_seen_utc=device.last_seen_utc,
        firmware_version=device.firmware_version or "v1.2.0-esp32s3",
        last_calibration_time=device.last_calibration_time,
        created_at=device.created_at,
        bound_at_utc=new_ownership.bound_at_utc,
    )


def list_user_devices(
    db: Session,
    user_id: int,
    skip: int = 0,
    limit: int = 100,
    cursor: str | None = None,
) -> list[DeviceResponse]:
    """Retrieve all devices bound to a user, joined with ownership for bound_at_utc."""
    query = (
        db.query(Device, DeviceOwnership.bound_at_utc)
        .join(DeviceOwnership, Device.device_id == DeviceOwnership.device_id)
        .filter(DeviceOwnership.user_id == user_id)
    )

    decoded = decode_cursor(cursor)
    if decoded is not None:
        cur_ts, cur_dev_id = decoded
        query = query.filter(
            or_(
                DeviceOwnership.bound_at_utc < cur_ts,
                and_(
                    DeviceOwnership.bound_at_utc == cur_ts,
                    Device.device_id < cur_dev_id,
                ),
            )
        )

    query = query.order_by(DeviceOwnership.bound_at_utc.desc(), Device.device_id.desc())

    if decoded is None and skip > 0:
        query = query.offset(skip)

    rows = query.limit(limit).all()
    return [
        DeviceResponse(
            uuid=device.uuid,
            device_id=device.device_id,
            name=device.name,
            status=device.status,
            is_online=(device.status == DeviceStatus.online),
            last_seen=device.last_seen_utc,
            last_seen_utc=device.last_seen_utc,
            firmware_version=device.firmware_version or "v1.2.0-esp32s3",
            last_calibration_time=device.last_calibration_time,
            created_at=device.created_at,
            bound_at_utc=bound_at_utc,
        )
        for device, bound_at_utc in rows
    ]


def list_all_devices(
    db: Session,
    skip: int = 0,
    limit: int = 100,
    cursor: str | None = None,
) -> list[DeviceResponse]:
    """Retrieve all registered devices for administrator overview."""
    query = (
        db.query(Device, DeviceOwnership.bound_at_utc)
        .outerjoin(DeviceOwnership, Device.device_id == DeviceOwnership.device_id)
    )

    decoded = decode_cursor(cursor)
    if decoded is not None:
        cur_ts, cur_dev_id = decoded
        query = query.filter(
            or_(
                Device.created_at < cur_ts,
                and_(
                    Device.created_at == cur_ts,
                    Device.device_id < cur_dev_id,
                ),
            )
        )

    query = query.order_by(Device.created_at.desc(), Device.device_id.desc())

    if decoded is None and skip > 0:
        query = query.offset(skip)

    rows = query.limit(limit).all()
    return [
        DeviceResponse(
            uuid=device.uuid,
            device_id=device.device_id,
            name=device.name,
            status=device.status,
            is_online=(device.status == DeviceStatus.online),
            last_seen=device.last_seen_utc,
            last_seen_utc=device.last_seen_utc,
            firmware_version=device.firmware_version or "v1.2.0-esp32s3",
            last_calibration_time=device.last_calibration_time,
            created_at=device.created_at,
            bound_at_utc=bound_at_utc or device.created_at,
        )
        for device, bound_at_utc in rows
    ]


def query_devices(
    db: Session,
    user: User,
    user_uuid: str | None = None,
    cursor: str | None = None,
    skip: int = 0,
    limit: int = 100,
) -> tuple[list[DeviceResponse], str | None]:
    """Retrieve devices with RBAC, optional target user resolution, and keyset cursor encoding."""
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
                devices: list[DeviceResponse] = []
            else:
                devices = list_user_devices(
                    db=db, user_id=target_user.id, skip=skip, limit=fetch_limit, cursor=cursor
                )
        else:
            devices = list_all_devices(
                db=db, skip=skip, limit=fetch_limit, cursor=cursor
            )
    else:
        devices = list_user_devices(
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

    return items, next_cursor


def _resolve_device_by_uuid(db: Session, device_uuid: UUID | str) -> Device:
    """Resolve a device strictly by its UUID, raising 404 if invalid or not found."""
    if not isinstance(device_uuid, UUID):
        try:
            device_uuid = UUID(str(device_uuid))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Device '{device_uuid}' not found",
            )
    device = db.query(Device).filter(Device.uuid == device_uuid).one_or_none()
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device '{device_uuid}' not found",
        )
    return device


def unbind_device(db: Session, user_id: int, device_uuid: UUID | str) -> None:
    """Remove device ownership for the given user strictly by device UUID."""
    device = _resolve_device_by_uuid(db, device_uuid)
    ownership = (
        db.query(DeviceOwnership)
        .filter_by(user_id=user_id, device_id=device.device_id)
        .one_or_none()
    )
    if ownership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not bound to this user",
        )
    db.delete(ownership)
    db.commit()


def unbind_device_admin(db: Session, device_uuid: UUID | str) -> None:
    """Remove all device ownerships for a device as an administrator strictly by device UUID."""
    device = _resolve_device_by_uuid(db, device_uuid)
    ownerships = db.query(DeviceOwnership).filter_by(device_id=device.device_id).all()
    if not ownerships:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not bound to any user",
        )
    for o in ownerships:
        db.delete(o)
    db.commit()


def resolve_user_by_identifier(db: Session, user_identifier: int | str | None) -> User | None:
    """Resolve a user by DB id, UUID string, digit string, OAuth sub, or email."""
    if user_identifier is None:
        return None
    if isinstance(user_identifier, int):
        return db.query(User).filter(User.id == user_identifier).first()
    if not isinstance(user_identifier, str):
        return None

    cleaned = user_identifier.strip()
    try:
        val_u = UUID(cleaned)
        user = db.query(User).filter(User.uuid == val_u).first()
        if user is not None:
            return user
    except (ValueError, TypeError, AttributeError):
        pass

    if cleaned.isdigit():
        user = db.query(User).filter(User.id == int(cleaned)).first()
        if user is not None:
            return user

    return (
        db.query(User)
        .filter(
            (User.google_sub == cleaned)
            | (User.azure_sub == cleaned)
            | (User.email == cleaned)
        )
        .first()
    )


def update_single_device_presence(
    db: Session,
    device_id: str,
    is_online: bool,
    timestamp: datetime | None = None,
) -> Device:
    """Update connection status and last seen timestamp for a single device."""
    device = db.query(Device).filter_by(device_id=device_id).one_or_none()
    if device is None:
        logger.warning("Device presence update rejected: device '%s' not found", device_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found",
        )

    if is_online:
        device.status = DeviceStatus.online
        device.last_seen_utc = timestamp or datetime.now(UTC)
        logger.info("Device '%s' marked online at %s", device.device_id, device.last_seen_utc)
    else:
        device.status = DeviceStatus.offline
        logger.info("Device '%s' marked offline", device.device_id)
        # Do not overwrite last_seen_utc on disconnect in order to preserve timestamp of last received heartbeat

    db.commit()
    db.refresh(device)
    logger.info("Relayed device presence update: device=%s, state=%s", device.device_id, device.status)
    return device


def disconnect_user_devices_on_lwt(db: Session, user_identifier: int | str | None) -> list[str]:
    """Mark all devices owned by the identified user as offline upon MQTT LWT disconnect."""
    user = resolve_user_by_identifier(db, user_identifier)
    if user is None:
        logger.warning("User '%s' not found for LWT presence update", user_identifier)
        db.commit()
        return []

    ownerships = db.query(DeviceOwnership).filter_by(user_id=user.id).all()
    device_ids = [o.device_id for o in ownerships]
    if device_ids:
        db.query(Device).filter(Device.device_id.in_(device_ids)).update(
            {Device.status: DeviceStatus.offline},
            synchronize_session=False,
        )
        logger.info(
            "User '%s' (id=%s) LWT triggered: marked %d device(s) offline: %s",
            user_identifier,
            user.id,
            len(device_ids),
            device_ids,
        )
    else:
        logger.info("User '%s' (id=%s) has no bound devices to update", user_identifier, user.id)

    db.commit()
    logger.info("Relayed user LWT presence update: user_id=%s, state=offline", user_identifier)
    return device_ids


# Backwards compatibility alias
handle_user_lwt_disconnect = disconnect_user_devices_on_lwt

