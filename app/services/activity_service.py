"""Service layer for querying device activity events and haptic vibration logs."""

from datetime import datetime

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.api.v1.utils import decode_cursor, encode_cursor, validate_and_normalize_date_range
from app.db.models import ActivityEvent, Device, DeviceOwnership, HapticLog, User, UserRole
from app.schemas.cloud import (
    ActivityEventPage,
    ActivityEventResponse,
    HapticLogPage,
    HapticLogResponse,
)
from app.services.device_service import verify_device_ownership


def resolve_target_device_ids(device_id: str, user: User, db: Session) -> list[str]:
    """Resolve and authorize target device IDs from comma-separated string or 'all'."""
    raw_devs = [d.strip() for d in device_id.split(",") if d.strip()]
    if not raw_devs or any(d.lower() == "all" for d in raw_devs):
        if user.role == UserRole.admin:
            return [d.device_id for d in db.query(Device.device_id).all()]
        return [o.device_id for o in db.query(DeviceOwnership.device_id).filter_by(user_id=user.id).all()]

    for dev_id in raw_devs:
        verify_device_ownership(db=db, user=user, device_id=dev_id, allow_clinician=False)
    return raw_devs


def query_activity_events(
    db: Session,
    user: User,
    device_id: str,
    event_type: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    raw_end: str | None = None,
    cursor: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[ActivityEventPage, str | None]:
    """Query activity events with device filtering, temporal isolation, type/date filtering and keyset pagination."""
    target_device_ids = resolve_target_device_ids(device_id=device_id, user=user, db=db)
    if not target_device_ids:
        return ActivityEventPage(items=[], page=page, page_size=page_size, total=0), None

    norm_start, norm_end = validate_and_normalize_date_range(start_date, end_date, raw_end)

    query = db.query(ActivityEvent).filter(ActivityEvent.device_id.in_(target_device_ids))

    # Temporal isolation for non-admin users: cannot view historical data from before device binding
    if user.role != UserRole.admin:
        ownerships = (
            db.query(DeviceOwnership.device_id, DeviceOwnership.bound_at_utc)
            .filter(DeviceOwnership.user_id == user.id, DeviceOwnership.device_id.in_(target_device_ids))
            .all()
        )
        if not ownerships:
            return ActivityEventPage(items=[], page=page, page_size=page_size, total=0), None
        bound_conds = []
        for dev_id, bound_at in ownerships:
            if bound_at is not None:
                bound_conds.append(and_(ActivityEvent.device_id == dev_id, ActivityEvent.timestamp_utc >= bound_at))
            else:
                bound_conds.append(ActivityEvent.device_id == dev_id)
        if bound_conds:
            query = query.filter(or_(*bound_conds))

    if event_type is not None and isinstance(event_type, str):
        clean_types = [t.strip() for t in event_type.split(",") if t.strip()]
        if clean_types and not any(t.lower() == "all" for t in clean_types):
            conditions = []
            exact_types = [t for t in clean_types if t.lower() != "falls"]
            has_falls = any(t.lower() == "falls" for t in clean_types)
            if exact_types:
                conditions.append(ActivityEvent.event_type.in_(exact_types))
            if has_falls:
                conditions.append(ActivityEvent.event_type.ilike("%fall%"))
            if conditions:
                query = query.filter(or_(*conditions))

    if norm_start is not None:
        query = query.filter(ActivityEvent.timestamp_utc >= norm_start)
    if norm_end is not None:
        query = query.filter(ActivityEvent.timestamp_utc <= norm_end)

    decoded = decode_cursor(cursor)
    if decoded is not None:
        cur_ts, cur_id_str = decoded
        try:
            cur_id = int(cur_id_str)
            query = query.filter(
                or_(
                    ActivityEvent.timestamp_utc < cur_ts,
                    and_(
                        ActivityEvent.timestamp_utc == cur_ts,
                        ActivityEvent.id < cur_id,
                    ),
                )
            )
        except ValueError:
            pass

    total = query.with_entities(func.count(ActivityEvent.id)).scalar() or 0
    query = query.order_by(ActivityEvent.timestamp_utc.desc(), ActivityEvent.id.desc())

    if decoded is None and page > 1:
        query = query.offset((page - 1) * page_size)

    events_plus_one = query.limit(page_size + 1).all()
    if len(events_plus_one) > page_size:
        events = events_plus_one[:page_size]
        next_cursor = encode_cursor(events[-1].timestamp_utc, events[-1].id)
    else:
        events = events_plus_one
        next_cursor = None

    user_owned_devs: set[str] = set()
    if user.role == UserRole.admin:
        user_owned_devs = {
            o.device_id
            for o in db.query(DeviceOwnership.device_id).filter_by(user_id=user.id).all()
        }

    items = [
        ActivityEventResponse(
            id=event.id,
            device_id=event.device_id,
            event_type=event.event_type,
            timestamp_utc=event.timestamp_utc,
            confidence_score=event.confidence_score,
            is_pseudonymized=(user.role == UserRole.admin and event.device_id not in user_owned_devs),
        )
        for event in events
    ]

    page_obj = ActivityEventPage(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
        next_cursor=next_cursor,
    )
    return page_obj, next_cursor


def query_haptic_logs(
    db: Session,
    user: User,
    device_id: str,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    raw_end: str | None = None,
    cursor: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[HapticLogPage, str | None]:
    """Query haptic command history with device filtering, temporal isolation, and keyset pagination."""
    target_device_ids = resolve_target_device_ids(device_id=device_id, user=user, db=db)
    if not target_device_ids:
        return HapticLogPage(items=[], page=page, page_size=page_size, total=0), None

    norm_start, norm_end = validate_and_normalize_date_range(start_date, end_date, raw_end)

    query = db.query(HapticLog).filter(HapticLog.device_id.in_(target_device_ids))

    # Temporal isolation for non-admin users: cannot view historical haptics from before device binding
    if user.role != UserRole.admin:
        ownerships = (
            db.query(DeviceOwnership.device_id, DeviceOwnership.bound_at_utc)
            .filter(DeviceOwnership.user_id == user.id, DeviceOwnership.device_id.in_(target_device_ids))
            .all()
        )
        if not ownerships:
            return HapticLogPage(items=[], page=page, page_size=page_size, total=0), None
        bound_conds = []
        for dev_id, bound_at in ownerships:
            if bound_at is not None:
                bound_conds.append(and_(HapticLog.device_id == dev_id, HapticLog.triggered_at_utc >= bound_at))
            else:
                bound_conds.append(HapticLog.device_id == dev_id)
        if bound_conds:
            query = query.filter(or_(*bound_conds))

    if norm_start is not None:
        query = query.filter(HapticLog.triggered_at_utc >= norm_start)
    if norm_end is not None:
        query = query.filter(HapticLog.triggered_at_utc <= norm_end)

    decoded = decode_cursor(cursor)
    if decoded is not None:
        cur_ts, cur_id_str = decoded
        try:
            cur_id = int(cur_id_str)
            query = query.filter(
                or_(
                    HapticLog.triggered_at_utc < cur_ts,
                    and_(
                        HapticLog.triggered_at_utc == cur_ts,
                        HapticLog.id < cur_id,
                    ),
                )
            )
        except ValueError:
            pass

    total = query.with_entities(func.count(HapticLog.id)).scalar() or 0
    query = query.order_by(HapticLog.triggered_at_utc.desc(), HapticLog.id.desc())

    if decoded is None and page > 1:
        query = query.offset((page - 1) * page_size)

    logs_plus_one = query.limit(page_size + 1).all()
    if len(logs_plus_one) > page_size:
        logs = logs_plus_one[:page_size]
        next_cursor = encode_cursor(logs[-1].triggered_at_utc, logs[-1].id)
    else:
        logs = logs_plus_one
        next_cursor = None

    page_obj = HapticLogPage(
        items=[HapticLogResponse.model_validate(log, from_attributes=True) for log in logs],
        page=page,
        page_size=page_size,
        total=total,
        next_cursor=next_cursor,
    )
    return page_obj, next_cursor
