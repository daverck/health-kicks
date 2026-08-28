"""Callable ingestion boundaries for IoT Rule, SQS, or worker messages."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Device, DeviceStatus, FallEvent, FallStatus, ProcessedMessage
from app.schemas.ingestion import IngestionEvent
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
    if not isinstance(body, dict):
        raise ValueError("payload must be a mapping")
    return header, body


def _get_device(session: Session, device_id: str, seen_at: datetime) -> Device:
    device = session.query(Device).filter_by(device_id=device_id).one_or_none()
    if device is None:
        device = Device(device_id=device_id)
        session.add(device)
    device.last_seen_utc = seen_at
    device.status = DeviceStatus.online
    return device


def ingest_fall_event(
    session: Session,
    message: dict[str, Any],
    headers: dict[str, Any] | None = None,
) -> FallEvent | None:
    """Normalize and persist a fall event, returning ``None`` for a duplicate msg_id."""
    header, payload = _parts(message, headers)
    device_id = str(header.get("device_id") or payload["device_id"])
    seen_at = _timestamp(header.get("timestamp_utc", payload.get("timestamp_utc")))
    msg_id = header.get("msg_id", payload.get("msg_id"))
    if msg_id is not None:
        if session.query(ProcessedMessage).filter_by(msg_id=str(msg_id)).first():
            return None
        session.add(ProcessedMessage(msg_id=str(msg_id)))
    _get_device(session, device_id, seen_at)
    event = FallEvent(
        device_id=device_id,
        timestamp_utc=_timestamp(payload.get("timestamp_utc", header.get("timestamp_utc"))),
        confidence_score=(
            float(payload.get("confidence_score", payload.get("confidence")))
            if payload.get("confidence_score", payload.get("confidence")) is not None
            else None
        ),
        raw_imu_json=payload.get("raw_imu_json", payload.get("imu", payload)),
        status_enum=FallStatus.detected,
    )
    session.add(event)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        if msg_id is not None and session.query(ProcessedMessage).filter_by(msg_id=str(msg_id)).first():
            return None
        raise
    session.refresh(event)
    return event


def ingest_event(session: Session, message: dict[str, Any]) -> FallEvent | None:
    """Validate and persist one AWS IoT Rule event with idempotent delivery."""
    contract = IngestionEvent.model_validate(message)
    header = contract.header
    payload = contract.payload
    if session.query(ProcessedMessage).filter_by(msg_id=header.msg_id).first():
        return None
    session.add(ProcessedMessage(msg_id=header.msg_id))
    _get_device(session, header.device_id, header.timestamp_utc)
    event = FallEvent(
        device_id=header.device_id,
        event_type=payload.event_type,
        timestamp_utc=header.timestamp_utc,
        confidence_score=payload.confidence_score,
        raw_imu_json=payload.raw_imu_snapshot,
        status_enum=FallStatus.detected,
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