"""User resolution and IoT downlink status dispatch for fall alerts."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import AlertContact, FallAlert, User
from app.services.aws_iot_service import AWSIoTPublishService
from app.services.fall_alert_tokens import _iso_utc

logger = logging.getLogger("healthkicks.fall_alert")


def build_downlink_status_payload(
    db: Session,
    alert: FallAlert,
    *,
    sms_command: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the MQTT downlink payload for healthkicks/v1/users/{user_id}/fall-alert/status."""
    contacts_total = 0
    if alert.contacts_snapshot_json:
        try:
            contacts_total = len(json.loads(alert.contacts_snapshot_json))
        except Exception:
            contacts_total = 0
    else:
        contacts_total = (
            db.query(func.count(AlertContact.id))
            .filter(AlertContact.user_id == alert.user_id)
            .scalar()
            or 0
        )

    acknowledged_by: dict[str, str] | None = None
    if alert.acknowledged_by_first_name:
        acknowledged_by = {
            "first_name": alert.acknowledged_by_first_name,
            "last_name": alert.acknowledged_by_last_name or "",
        }

    payload: dict[str, Any] = {
        "event_id": alert.event_id,
        "alert_id": alert.id,
        "state": alert.state,
        "current_contact_rank": alert.current_contact_rank,
        "contacts_total": int(contacts_total),
        "acknowledged_by": acknowledged_by,
        "cancel_reason": alert.cancel_reason,
        "next_action_at": _iso_utc(alert.next_action_at_utc),
        "updated_at": _iso_utc(alert.updated_at_utc or datetime.now(UTC)),
    }
    if sms_command is not None:
        payload["sms_command"] = sms_command
    return payload


def _publish_status_safe(
    db: Session,
    alert: FallAlert,
    iot_publisher: AWSIoTPublishService | None,
    *,
    sms_command: dict[str, Any] | None = None,
) -> bool:
    """Safely publish the current fall alert state machine status over AWS IoT Core MQTT."""
    if iot_publisher is None:
        return False
    try:
        payload = build_downlink_status_payload(db, alert, sms_command=sms_command)
        user = alert.user or db.query(User).filter(User.id == alert.user_id).first()
        target_sub = str(user.uuid) if (user and getattr(user, "uuid", None)) else str(alert.user_id)
        return bool(iot_publisher.publish_fall_alert_status(target_sub, payload))
    except Exception:
        logger.exception("Failed to publish downlink fall alert status for alert %s", alert.id)
        return False


def resolve_user_for_ingest(db: Session, raw_user_id: int | str | uuid.UUID) -> User:
    """Resolve user from UUID, integer ID, numeric string, SSO subject, or email."""
    user: User | None = None
    if isinstance(raw_user_id, uuid.UUID):
        user = db.query(User).filter(User.uuid == raw_user_id).first()
    elif isinstance(raw_user_id, int):
        user = db.query(User).filter(User.id == raw_user_id).first()
    elif isinstance(raw_user_id, str):
        cleaned = raw_user_id.strip()
        try:
            val_uuid = uuid.UUID(cleaned)
            user = db.query(User).filter(User.uuid == val_uuid).first()
        except (ValueError, TypeError, AttributeError):
            pass

        if user is None and cleaned.isdigit():
            try:
                user = db.query(User).filter(User.id == int(cleaned)).first()
            except (ValueError, TypeError):
                pass

        if user is None:
            user = (
                db.query(User)
                .filter(
                    (User.google_sub == cleaned)
                    | (User.azure_sub == cleaned)
                    | (User.email == cleaned)
                )
                .first()
            )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{raw_user_id}' not found",
        )
    return user


def _apply_location_to_alert(alert: FallAlert, location: Any) -> None:
    """Store GPS coordinates and fix timestamp onto the FallAlert model record."""
    if location is None:
        return
    alert.lat = location.lat
    alert.lon = location.lon
    alert.accuracy_m = location.accuracy_m
    if location.captured_at is not None:
        alert.location_captured_at_utc = datetime.fromtimestamp(
            location.captured_at / 1000.0, tz=UTC
        )
    elif alert.location_captured_at_utc is None:
        alert.location_captured_at_utc = datetime.now(UTC)
