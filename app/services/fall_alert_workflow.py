"""State machine transitions and escalation loop for fall alerts."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import (
    AlertContact,
    FallAlert,
    FallAlertNotification,
    FallAlertNotificationStatus,
    FallAlertState,
    User,
)
from app.services.aws_iot_service import AWSIoTPublishService
from app.services.email_service import SmtpEmailChannel
from app.services.fall_alert_notifier import (
    build_sms_notification_command,
    send_contact_email_notification,
)
from app.services.fall_alert_resolution import _publish_status_safe
from app.services.fall_alert_tokens import (
    AckTokenError,
    _ensure_utc,
    verify_ack_token,
)

logger = logging.getLogger("healthkicks.fall_alert.workflow")

ACTIVE_ALERT_STATES = (FallAlertState.PENDING.value, FallAlertState.ESCALATING.value)


def start_or_advance_escalation(
    db: Session,
    alert: FallAlert,
    now_utc: datetime,
    email_channel: SmtpEmailChannel,
    iot_publisher: AWSIoTPublishService | None,
    cfg: Settings,
) -> None:
    """Transition PENDING -> ESCALATING/EXHAUSTED or advance ESCALATING to the next contact."""
    if alert.state == FallAlertState.PENDING.value:
        contacts = (
            db.query(AlertContact)
            .filter(AlertContact.user_id == alert.user_id)
            .order_by(AlertContact.priority.asc(), AlertContact.id.asc())
            .all()
        )
        snapshot = [
            {
                "contact_id": c.id,
                "first_name": c.first_name,
                "last_name": c.last_name,
                "email": c.email,
                "phone": c.phone,
                "rank": idx,
            }
            for idx, c in enumerate(contacts, start=1)
        ]
        alert.contacts_snapshot_json = json.dumps(snapshot)

        if not snapshot:
            alert.state = FallAlertState.EXHAUSTED.value
            alert.closed_at_utc = now_utc
            alert.next_action_at_utc = None
            alert.updated_at_utc = now_utc
            db.commit()
            db.refresh(alert)
            _publish_status_safe(db, alert, iot_publisher)
            return

        alert.state = FallAlertState.ESCALATING.value
        alert.escalation_started_at_utc = now_utc
        alert.current_contact_rank = 0
        alert.next_action_at_utc = now_utc + timedelta(
            seconds=cfg.fall_alert_escalation_step_sec
        )
        alert.updated_at_utc = now_utc
        db.commit()

    if alert.state != FallAlertState.ESCALATING.value:
        return

    try:
        snapshot_list: list[dict[str, Any]] = json.loads(alert.contacts_snapshot_json or "[]")
    except Exception:
        snapshot_list = []

    total_contacts = len(snapshot_list)
    next_rank = alert.current_contact_rank + 1

    user = alert.user or db.query(User).filter(User.id == alert.user_id).first()
    patient_name = (
        (user.name if user and user.name else (user.email if user else None))
        or f"Patient #{alert.user_id}"
    )

    while next_rank <= total_contacts:
        contact_entry = snapshot_list[next_rank - 1]
        contact_phone: str | None = contact_entry.get("phone")

        # 1. Email notification channel
        _, email_sent = send_contact_email_notification(
            db=db,
            alert=alert,
            contact_entry=contact_entry,
            next_rank=next_rank,
            total_contacts=total_contacts,
            patient_name=patient_name,
            now_utc=now_utc,
            email_channel=email_channel,
            cfg=cfg,
        )

        # 2. SMS notification channel
        sms_notif: FallAlertNotification | None = None
        sms_command: dict[str, Any] | None = None
        if contact_phone:
            sms_notif, sms_command = build_sms_notification_command(
                db=db,
                alert=alert,
                contact_entry=contact_entry,
                next_rank=next_rank,
                patient_name=patient_name,
                now_utc=now_utc,
                cfg=cfg,
            )

        # 3. Evaluate whether this contact rank was reached via at least one channel
        if email_sent or sms_command is not None:
            alert.current_contact_rank = next_rank
            alert.next_action_at_utc = now_utc + timedelta(
                seconds=cfg.fall_alert_escalation_step_sec
            )
            alert.updated_at_utc = now_utc
            db.commit()
            db.refresh(alert)

            mqtt_ok = _publish_status_safe(
                db,
                alert,
                iot_publisher,
                sms_command=sms_command,
            )
            if sms_notif is not None and not mqtt_ok:
                sms_notif.status = FallAlertNotificationStatus.FAILED.value
                sms_notif.error_message = "MQTT downlink publish failed"
                db.commit()
                db.refresh(alert)
                if not email_sent:
                    next_rank += 1
                    continue
            return

        alert.current_contact_rank = next_rank
        alert.updated_at_utc = now_utc
        db.flush()
        next_rank += 1

    alert.state = FallAlertState.EXHAUSTED.value
    alert.closed_at_utc = now_utc
    alert.next_action_at_utc = None
    alert.updated_at_utc = now_utc
    db.commit()
    db.refresh(alert)
    _publish_status_safe(db, alert, iot_publisher)


def process_due_fall_alerts(
    db: Session,
    *,
    now_utc: datetime | None = None,
    email_channel: SmtpEmailChannel | None = None,
    iot_publisher: AWSIoTPublishService | None = None,
    config: Settings | None = None,
) -> int:
    """Process all PENDING and ESCALATING alerts whose next_action_at_utc has elapsed."""
    cfg = config or get_settings()
    now = _ensure_utc(now_utc) or datetime.now(UTC)
    channel = email_channel or SmtpEmailChannel(config=cfg)

    query = (
        db.query(FallAlert)
        .filter(
            FallAlert.state.in_(ACTIVE_ALERT_STATES),
            FallAlert.next_action_at_utc.is_not(None),
            FallAlert.next_action_at_utc <= now,
        )
        .order_by(FallAlert.next_action_at_utc.asc(), FallAlert.id.asc())
    )

    bind = db.get_bind()
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect_name == "postgresql":
        query = query.with_for_update(of=FallAlert, skip_locked=True)

    due_alerts = query.all()
    processed_count = 0
    for alert in due_alerts:
        try:
            start_or_advance_escalation(
                db=db,
                alert=alert,
                now_utc=now,
                email_channel=channel,
                iot_publisher=iot_publisher,
                cfg=cfg,
            )
            processed_count += 1
        except Exception:
            logger.exception("Error processing due fall alert id=%s", alert.id)
            db.rollback()

    return processed_count


def get_alert_and_notification_for_token(
    db: Session,
    token: str,
    *,
    config: Settings | None = None,
) -> tuple[FallAlert, FallAlertNotification]:
    """Validate token and load the corresponding FallAlert and FallAlertNotification."""
    alert_id, notification_id = verify_ack_token(token, config=config)
    alert = db.query(FallAlert).filter(FallAlert.id == alert_id).one_or_none()
    if alert is None:
        raise AckTokenError("Alerte de chute introuvable.", expired=False)
    notif = (
        db.query(FallAlertNotification)
        .filter(
            FallAlertNotification.id == notification_id,
            FallAlertNotification.alert_id == alert.id,
        )
        .one_or_none()
    )
    if notif is None:
        raise AckTokenError("Notification d'alerte introuvable.", expired=False)
    return alert, notif


def acknowledge_fall_alert(
    db: Session,
    token: str,
    *,
    now_utc: datetime | None = None,
    iot_publisher: AWSIoTPublishService | None = None,
    config: Settings | None = None,
) -> tuple[FallAlert, FallAlertNotification, bool]:
    """Acknowledge a fall alert from a verified signed token."""
    now = _ensure_utc(now_utc) or datetime.now(UTC)
    alert, notif = get_alert_and_notification_for_token(db, token, config=config)

    if alert.state in (FallAlertState.ESCALATING.value, FallAlertState.EXHAUSTED.value):
        alert.state = FallAlertState.ACKNOWLEDGED.value
        alert.acknowledged_by_first_name = notif.contact_first_name
        alert.acknowledged_by_last_name = notif.contact_last_name
        alert.acknowledged_by_contact_id = notif.contact_id
        alert.closed_at_utc = now
        alert.next_action_at_utc = None
        alert.updated_at_utc = now

        notif.status = FallAlertNotificationStatus.ACKNOWLEDGED.value
        notif.acknowledged_at_utc = now

        db.commit()
        db.refresh(alert)
        db.refresh(notif)
        _publish_status_safe(db, alert, iot_publisher)
        return alert, notif, True

    return alert, notif, False
