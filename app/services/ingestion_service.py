from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ActivityEvent, Device, DeviceOwnership, DeviceStatus, ProcessedMessage, StudioSession, User, UserRole
from app.schemas.ingestion import DeviceStatusEvent, IngestionEvent


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _parts(message: dict[str, Any], headers: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    header = dict(headers or message.get("header") or message.get("headers") or {})
    body = message.get("payload", message)
    payload = dict(body if isinstance(body, dict) else {})
    return header, payload


def _get_device(session: Session, device_id: str, seen_at: datetime) -> Device:
    device = session.query(Device).filter_by(device_id=device_id).first()
    if not device:
        device = Device(device_id=device_id, name=f"Device {device_id}", status=DeviceStatus.online)
        session.add(device)
    device.status = DeviceStatus.online
    device.last_seen_utc = seen_at
    return device


def ingest_event(session: Session, message: dict[str, Any]) -> ActivityEvent | None:
    """Validate and persist one AWS IoT Rule event with idempotent delivery and device ownership check."""
    contract = IngestionEvent.model_validate(message)
    header = contract.header
    payload = contract.payload

    # Verify device ownership
    ownership = session.query(DeviceOwnership).filter_by(device_id=header.device_id).first()
    if ownership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: device not bound to any user",
        )

    if session.query(ProcessedMessage).filter_by(msg_id=header.msg_id).first():
        return None
    session.add(ProcessedMessage(msg_id=header.msg_id))
    _get_device(session, header.device_id, header.timestamp_utc)
    event = ActivityEvent(
        device_id=header.device_id,
        event_type=payload.event_type,
        timestamp_utc=header.timestamp_utc,
        confidence_score=payload.confidence_score,
    )
    session.add(event)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        if session.query(ProcessedMessage).filter_by(msg_id=header.msg_id).first():
            return None
        raise
    session.refresh(event)
    return event


def ingest_device_status(
    session: Session,
    message: dict[str, Any],
    headers: dict[str, Any] | None = None,
) -> Device:
    """Normalize a device status packet and update its online/offline presence."""
    if headers is not None:
        message = {"header": headers, "payload": message}
    contract = DeviceStatusEvent.model_validate(message)
    device = _get_device(
        session,
        contract.header.device_id,
        contract.header.timestamp_utc or datetime.now(timezone.utc),
    )
    device.status = DeviceStatus(contract.payload.status)
    session.commit()
    session.refresh(device)
    return device


def ingest_raw_telemetry(
    session: Session,
    message: dict[str, Any],
    headers: dict[str, Any] | None = None,
) -> StudioSession | None:
    """Ingest a raw IMU telemetry batch and update the StudioSession sample_count in Aurora DB."""
    header, payload = _parts(message, headers)
    device_id = str(payload.get("device_id") or header.get("device_id") or "")
    session_id_raw = payload.get("session_id") or header.get("session_id")
    if not session_id_raw:
        return None

    try:
        sess_uuid = UUID(str(session_id_raw))
    except (ValueError, TypeError):
        return None

    readings = payload.get("readings") or []
    sample_count = int(payload.get("sample_count", len(readings)))

    studio_session = session.query(StudioSession).filter_by(id=sess_uuid).first()
    if studio_session is not None:
        session_user = session.query(User).filter_by(id=studio_session.user_id).first()
        is_admin = session_user is not None and session_user.role == UserRole.admin

        if not is_admin:
            # Non-admin: verify device belongs to the session user
            ownership = (
                session.query(DeviceOwnership)
                .filter_by(user_id=studio_session.user_id, device_id=device_id)
                .first()
            )
            if ownership is None and studio_session.device_id != device_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Forbidden: device does not belong to session user",
                )

        studio_session.sample_count = sample_count
        if "label" in payload and payload["label"]:
            studio_session.label = payload["label"]
        session.commit()
        session.refresh(studio_session)
        return studio_session

    # If session record does not exist yet in Aurora, verify device ownership
    ownership = session.query(DeviceOwnership).filter_by(device_id=device_id).first() if device_id else None
    if ownership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: device not bound to any user",
        )

    user_id = ownership.user_id

    studio_session = StudioSession(
        id=sess_uuid,
        user_id=user_id,
        device_id=device_id or "unknown",
        label=str(payload.get("label", "unlabeled")),
        sample_count=sample_count,
        duration_sec=float(payload.get("duration_sec", 5.0)),
        is_validated=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(studio_session)
    session.commit()
    session.refresh(studio_session)
    return studio_session
