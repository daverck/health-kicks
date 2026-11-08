"""Notification formatting, SMTP escalation dispatch, and test message builders for fall alerting."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import (
    AlertContact,
    FallAlert,
    FallAlertNotification,
    FallAlertNotificationStatus,
    User,
)
from app.schemas.fall_alert import AlertContactTestEmailResponse
from app.services.aws_iot_service import AWSIoTPublishService
from app.services.email_service import (
    SmtpEmailChannel,
    build_fall_alert_sms_body,
    build_test_sms_body,
    format_paris_datetime,
)
from app.services.fall_alert_tokens import (
    _ensure_utc,
    _iso_utc,
    build_ack_url,
)

logger = logging.getLogger("healthkicks.fall_alert.notifier")


def send_contact_email_notification(
    db: Session,
    alert: FallAlert,
    contact_entry: dict[str, Any],
    next_rank: int,
    total_contacts: int,
    patient_name: str,
    now_utc: datetime,
    email_channel: SmtpEmailChannel,
    cfg: Settings,
) -> tuple[FallAlertNotification, bool]:
    """Create and dispatch an email notification for a fall alert contact."""
    email_notif = FallAlertNotification(
        alert_id=alert.id,
        rank=next_rank,
        channel="email",
        contact_id=contact_entry.get("contact_id"),
        contact_first_name=contact_entry["first_name"],
        contact_last_name=contact_entry["last_name"],
        contact_email=contact_entry["email"],
        contact_phone=contact_entry.get("phone"),
        status=FallAlertNotificationStatus.SENT.value,
        sent_at_utc=now_utc,
    )
    db.add(email_notif)
    db.flush()

    email_ack_url = build_ack_url(alert.id, email_notif.id, config=cfg)
    email_sent = False
    try:
        email_channel.send_fall_alert_email(
            recipient_email=contact_entry["email"],
            contact_first_name=contact_entry["first_name"],
            contact_last_name=contact_entry["last_name"],
            patient_name=patient_name,
            device_id=alert.device_id,
            event_type=alert.event_type,
            confidence_score=alert.confidence_score,
            detected_at_utc=_ensure_utc(alert.detected_at_utc) or now_utc,
            rank=next_rank,
            total_contacts=total_contacts,
            ack_url=email_ack_url,
            lat=alert.lat,
            lon=alert.lon,
            accuracy_m=alert.accuracy_m,
        )
        email_notif.status = FallAlertNotificationStatus.SENT.value
        email_notif.sent_at_utc = now_utc
        email_sent = True
    except Exception as exc:
        logger.error(
            "SMTP escalation failed for alert %s rank %d (%s): %s",
            alert.id,
            next_rank,
            contact_entry["email"],
            exc,
        )
        email_notif.status = FallAlertNotificationStatus.FAILED.value
        email_notif.sent_at_utc = now_utc
        email_notif.error_message = str(exc)[:512]
        db.flush()

    return email_notif, email_sent


def build_sms_notification_command(
    db: Session,
    alert: FallAlert,
    contact_entry: dict[str, Any],
    next_rank: int,
    patient_name: str,
    now_utc: datetime,
    cfg: Settings,
) -> tuple[FallAlertNotification, dict[str, Any]]:
    """Create a pending SMS notification record and format the MQTT downlink command."""
    contact_phone: str = contact_entry["phone"]
    sms_notif = FallAlertNotification(
        alert_id=alert.id,
        rank=next_rank,
        channel="sms",
        contact_id=contact_entry.get("contact_id"),
        contact_first_name=contact_entry["first_name"],
        contact_last_name=contact_entry["last_name"],
        contact_email=contact_entry["email"],
        contact_phone=contact_phone,
        status=FallAlertNotificationStatus.PENDING_SMS.value,
        sent_at_utc=now_utc,
    )
    db.add(sms_notif)
    db.flush()

    sms_ack_url = build_ack_url(alert.id, sms_notif.id, config=cfg)
    maps_url = (
        f"https://www.google.com/maps?q={alert.lat:.6f},{alert.lon:.6f}"
        if (alert.lat is not None and alert.lon is not None)
        else None
    )
    detected_at_str = format_paris_datetime(_ensure_utc(alert.detected_at_utc) or now_utc)
    sms_body = build_fall_alert_sms_body(
        patient_name=patient_name,
        detected_at_str=detected_at_str,
        ack_url=sms_ack_url,
        maps_url=maps_url,
    )
    sms_command = {
        "notification_id": sms_notif.id,
        "contact_id": contact_entry.get("contact_id"),
        "contact_first_name": contact_entry["first_name"],
        "recipient_phone": contact_phone,
        "message": sms_body,
        "is_test": False,
    }
    return sms_notif, sms_command


def dispatch_test_email_and_sms(
    db: Session,
    user: User,
    contact: AlertContact,
    *,
    email_channel: SmtpEmailChannel | None = None,
    iot_publisher: AWSIoTPublishService | None = None,
    config: Settings | None = None,
) -> AlertContactTestEmailResponse:
    """Send test email via SMTP and trigger optional test SMS command via MQTT."""
    cfg = config or get_settings()
    channel = email_channel or SmtpEmailChannel(config=cfg)
    patient_name = user.name or user.email or f"Utilisateur #{user.id}"

    try:
        channel.send_test_email(
            recipient_email=contact.email,
            contact_first_name=contact.first_name,
            contact_last_name=contact.last_name,
            patient_name=patient_name,
            priority=contact.priority,
        )
    except Exception as exc:
        logger.error(
            "Failed to send test email to contact %s (%s): %s", contact.id, contact.email, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to send test email via SMTP server",
        ) from exc

    now = datetime.now(UTC)
    contact.last_test_email_at = now
    contact.updated_at = now
    db.commit()
    db.refresh(contact)

    sms_dispatched = False
    if contact.phone and iot_publisher is not None:
        sms_body = build_test_sms_body(
            patient_name=patient_name,
            priority=contact.priority,
        )
        test_payload: dict[str, Any] = {
            "event_id": f"test-sms-{contact.id}-{int(now.timestamp())}",
            "alert_id": 0,
            "state": "TEST_SMS",
            "current_contact_rank": contact.priority,
            "contacts_total": 1,
            "acknowledged_by": None,
            "cancel_reason": None,
            "next_action_at": None,
            "updated_at": _iso_utc(now),
            "sms_command": {
                "notification_id": None,
                "contact_id": contact.id,
                "contact_first_name": contact.first_name,
                "recipient_phone": contact.phone,
                "message": sms_body,
                "is_test": True,
            },
        }
        try:
            target_sub = str(user.uuid) if getattr(user, "uuid", None) else str(user.id)
            sms_dispatched = bool(iot_publisher.publish_fall_alert_status(target_sub, test_payload))
        except Exception:
            logger.exception(
                "Failed to publish test SMS command for contact %s (user %s)",
                contact.id,
                user.id,
            )
            sms_dispatched = False

    return AlertContactTestEmailResponse(
        status="sent",
        contact_id=contact.id,
        sent_at=now,
        sms_dispatched=sms_dispatched,
    )
