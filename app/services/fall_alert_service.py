"""Business logic and state machine for fall alerting, emergency contacts, SMTP email, and mobile SMS escalation."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.pseudonymization import get_subject_pseudonym, mask_email
from app.db.models import (
    AlertContact,
    FallAlert,
    FallAlertAction,
    FallAlertCancelReason,
    FallAlertDelivery,
    FallAlertNotification,
    FallAlertNotificationStatus,
    FallAlertState,
    User,
)
from app.schemas.fall_alert import (
    AlertContactCreate,
    AlertContactResponse,
    AlertContactsOrderPayload,
    AlertContactTestEmailResponse,
    AlertContactUpdate,
    FallAlertAcknowledgedBy,
    FallAlertDetail,
    FallAlertIngestPayload,
    FallAlertIngestResponse,
    FallAlertLocationResponse,
    FallAlertSmsStatusRequest,
    FallAlertNotificationResponse,
    FallAlertSummary,
)
from app.services.aws_iot_service import AWSIoTPublishService
from app.services.email_service import (
    SmtpEmailChannel,
    build_fall_alert_sms_body,
    build_test_sms_body,
    format_paris_datetime,
)

logger = logging.getLogger("healthkicks.fall_alert")

ACK_TOKEN_SALT = "healthkicks-fall-alert-ack-v1"
ACTIVE_ALERT_STATES = (FallAlertState.PENDING.value, FallAlertState.ESCALATING.value)


class AckTokenError(Exception):
    """Raised when a fall alert acknowledgment token is invalid or expired."""

    def __init__(self, message: str, *, expired: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.expired = expired


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _iso_utc(dt: datetime | None) -> str | None:
    norm = _ensure_utc(dt)
    if norm is None:
        return None
    return norm.isoformat().replace("+00:00", "Z")


def _mask_last_name(last_name: str | None) -> str:
    if not last_name:
        return ""
    cleaned = last_name.strip()
    return f"{cleaned[0].upper()}." if cleaned else ""


def _mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    cleaned = phone.strip()
    if not cleaned:
        return None
    if len(cleaned) <= 4:
        return "*" * len(cleaned)
    prefix_len = 3 if cleaned.startswith("+") else 2
    if len(cleaned) <= prefix_len + 2:
        return cleaned[:1] + "*" * (len(cleaned) - 2) + cleaned[-1:]
    return f"{cleaned[:prefix_len]}{'*' * (len(cleaned) - prefix_len - 2)}{cleaned[-2:]}"


def generate_ack_token(
    alert_id: int,
    notification_id: int,
    config: Settings | None = None,
) -> str:
    """Create an HMAC-signed timed token for acknowledging a fall alert."""
    cfg = config or get_settings()
    serializer = URLSafeTimedSerializer(
        secret_key=cfg.fall_alert_ack_secret,
        salt=ACK_TOKEN_SALT,
    )
    return str(serializer.dumps({"a": int(alert_id), "n": int(notification_id)}))


def verify_ack_token(
    token: str,
    config: Settings | None = None,
) -> tuple[int, int]:
    """Verify signature and TTL of an acknowledgment token, returning (alert_id, notification_id)."""
    if not token or not token.strip():
        raise AckTokenError("Jeton de prise en charge manquant.", expired=False)
    cfg = config or get_settings()
    serializer = URLSafeTimedSerializer(
        secret_key=cfg.fall_alert_ack_secret,
        salt=ACK_TOKEN_SALT,
    )
    try:
        data = serializer.loads(token.strip(), max_age=cfg.fall_alert_ack_token_ttl_sec)
    except SignatureExpired as exc:
        raise AckTokenError(
            "Ce lien de prise en charge a expiré (validité maximale : 2 heures).",
            expired=True,
        ) from exc
    except BadSignature as exc:
        raise AckTokenError(
            "Ce lien de prise en charge est invalide ou altéré.",
            expired=False,
        ) from exc

    if not isinstance(data, dict) or "a" not in data or "n" not in data:
        raise AckTokenError("Structure de jeton invalide.", expired=False)
    try:
        return int(data["a"]), int(data["n"])
    except (TypeError, ValueError) as exc:
        raise AckTokenError("Identifiants de jeton invalides.", expired=False) from exc


def build_ack_url(
    alert_id: int,
    notification_id: int,
    config: Settings | None = None,
) -> str:
    """Build the full public URL for the emergency contact acknowledgment page on the frontend."""
    cfg = config or get_settings()
    token = generate_ack_token(alert_id, notification_id, config=cfg)
    base = cfg.frontend_url.rstrip("/")
    return f"{base}/fall-alerts/ack?token={token}"


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
    if iot_publisher is None:
        return False
    try:
        payload = build_downlink_status_payload(db, alert, sms_command=sms_command)
        return bool(iot_publisher.publish_fall_alert_status(alert.user_id, payload))
    except Exception:
        logger.exception("Failed to publish downlink fall alert status for alert %s", alert.id)
        return False


def resolve_user_for_ingest(db: Session, raw_user_id: int | str) -> User:
    """Resolve user from integer ID, numeric string, SSO subject, or email."""
    user: User | None = None
    if isinstance(raw_user_id, int):
        user = db.query(User).filter(User.id == raw_user_id).first()
    elif isinstance(raw_user_id, str):
        cleaned = raw_user_id.strip()
        if cleaned.isdigit():
            user = db.query(User).filter(User.id == int(cleaned)).first()
        else:
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


def _start_or_advance_escalation(
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
        alert.updated_at_utc = now_utc
        db.flush()

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
        email_notif = FallAlertNotification(
            alert_id=alert.id,
            rank=next_rank,
            channel="email",
            contact_id=contact_entry.get("contact_id"),
            contact_first_name=contact_entry["first_name"],
            contact_last_name=contact_entry["last_name"],
            contact_email=contact_entry["email"],
            contact_phone=contact_phone,
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

        # 2. SMS notification channel (when phone is configured on this contact)
        sms_notif: FallAlertNotification | None = None
        sms_command: dict[str, Any] | None = None
        if contact_phone:
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
                    # Neither email nor SMS succeeded for this contact -> try next contact immediately
                    next_rank += 1
                    continue
            return

        # Both email failed and no phone configured -> immediately try next contact
        alert.current_contact_rank = next_rank
        alert.updated_at_utc = now_utc
        db.flush()
        next_rank += 1

    # All contacts have been notified and the last step expired (or all failed)
    alert.state = FallAlertState.EXHAUSTED.value
    alert.closed_at_utc = now_utc
    alert.next_action_at_utc = None
    alert.updated_at_utc = now_utc
    db.commit()
    db.refresh(alert)
    _publish_status_safe(db, alert, iot_publisher)


def ingest_fall_alert_event(
    db: Session,
    payload: FallAlertIngestPayload,
    *,
    email_channel: SmtpEmailChannel | None = None,
    iot_publisher: AWSIoTPublishService | None = None,
    config: Settings | None = None,
    now_utc: datetime | None = None,
) -> FallAlertIngestResponse:
    """Process an incoming DETECTED, CONFIRM, CANCEL, or SMS_STATUS fall alert event."""
    cfg = config or get_settings()
    now = _ensure_utc(now_utc) or datetime.now(UTC)
    channel = email_channel or SmtpEmailChannel(config=cfg)
    user = resolve_user_for_ingest(db, payload.user_id)

    if payload.action == FallAlertAction.DETECTED:
        existing_by_event = (
            db.query(FallAlert).filter(FallAlert.event_id == payload.event_id).one_or_none()
        )
        if existing_by_event is not None:
            return FallAlertIngestResponse(
                status="ok",
                alert_id=existing_by_event.id,
                event_id=existing_by_event.event_id,
                state=existing_by_event.state,
                duplicate=True,
            )

        active_alert = (
            db.query(FallAlert)
            .filter(
                FallAlert.user_id == user.id,
                FallAlert.state.in_(ACTIVE_ALERT_STATES),
            )
            .order_by(FallAlert.id.desc())
            .first()
        )
        if active_alert is not None:
            if payload.location is not None and active_alert.lat is None:
                _apply_location_to_alert(active_alert, payload.location)
                active_alert.updated_at_utc = now
                db.commit()
                db.refresh(active_alert)
            return FallAlertIngestResponse(
                status="ok",
                alert_id=active_alert.id,
                event_id=active_alert.event_id,
                state=active_alert.state,
                duplicate=True,
            )

        assert payload.detected_at is not None
        assert payload.device_id is not None
        assert payload.event_type is not None
        assert payload.confidence is not None
        assert payload.delivery is not None

        detected_dt = datetime.fromtimestamp(payload.detected_at / 1000.0, tz=UTC)
        delay_sec = (now - detected_dt).total_seconds()
        is_suppressed = (
            payload.delivery == FallAlertDelivery.replayed
            or delay_sec > cfg.fall_alert_replay_threshold_sec
        )

        if is_suppressed:
            alert = FallAlert(
                event_id=payload.event_id,
                user_id=user.id,
                device_id=payload.device_id,
                event_type=payload.event_type,
                confidence_score=payload.confidence,
                delivery=payload.delivery.value,
                state=FallAlertState.SUPPRESSED.value,
                detected_at_utc=detected_dt,
                next_action_at_utc=None,
                closed_at_utc=now,
                created_at_utc=now,
                updated_at_utc=now,
            )
            _apply_location_to_alert(alert, payload.location)
            db.add(alert)
            db.commit()
            db.refresh(alert)
            return FallAlertIngestResponse(
                status="ok",
                alert_id=alert.id,
                event_id=alert.event_id,
                state=alert.state,
                duplicate=False,
            )

        alert = FallAlert(
            event_id=payload.event_id,
            user_id=user.id,
            device_id=payload.device_id,
            event_type=payload.event_type,
            confidence_score=payload.confidence,
            delivery=payload.delivery.value,
            state=FallAlertState.PENDING.value,
            detected_at_utc=detected_dt,
            next_action_at_utc=now + timedelta(seconds=cfg.fall_alert_cancel_window_sec),
            created_at_utc=now,
            updated_at_utc=now,
        )
        _apply_location_to_alert(alert, payload.location)
        db.add(alert)
        db.commit()
        db.refresh(alert)
        _publish_status_safe(db, alert, iot_publisher)
        return FallAlertIngestResponse(
            status="ok",
            alert_id=alert.id,
            event_id=alert.event_id,
            state=alert.state,
            duplicate=False,
        )

    if payload.action == FallAlertAction.CONFIRM:
        alert = (
            db.query(FallAlert)
            .filter(FallAlert.user_id == user.id, FallAlert.event_id == payload.event_id)
            .one_or_none()
        )
        if alert is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Fall alert '{payload.event_id}' not found",
            )
        if payload.location is not None:
            _apply_location_to_alert(alert, payload.location)
            alert.updated_at_utc = now
            db.flush()

        if alert.state == FallAlertState.PENDING.value:
            _start_or_advance_escalation(
                db=db,
                alert=alert,
                now_utc=now,
                email_channel=channel,
                iot_publisher=iot_publisher,
                cfg=cfg,
            )
            return FallAlertIngestResponse(
                status="ok",
                alert_id=alert.id,
                event_id=alert.event_id,
                state=alert.state,
                duplicate=False,
            )

        db.commit()
        return FallAlertIngestResponse(
            status="ok",
            alert_id=alert.id,
            event_id=alert.event_id,
            state=alert.state,
            duplicate=True,
        )

    if payload.action == FallAlertAction.SMS_STATUS:
        assert payload.notification_id is not None
        assert payload.sms_status is not None
        notif_query = (
            db.query(FallAlertNotification)
            .join(FallAlert, FallAlertNotification.alert_id == FallAlert.id)
            .filter(FallAlertNotification.id == payload.notification_id)
        )
        if payload.event_id:
            notif = notif_query.filter(
                (FallAlert.user_id == user.id) | (FallAlert.event_id == payload.event_id)
            ).first()
        else:
            notif = notif_query.filter(FallAlert.user_id == user.id).first()

        if notif is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Fall alert notification '{payload.notification_id}' not found",
            )
        alert = notif.alert
        if notif.status != FallAlertNotificationStatus.ACKNOWLEDGED.value:
            notif.status = payload.sms_status
            notif.error_message = payload.error_message
            if payload.sent_at:
                try:
                    notif.sent_at_utc = datetime.fromtimestamp(payload.sent_at / 1000.0, tz=UTC)
                except Exception:
                    notif.sent_at_utc = now
            else:
                notif.sent_at_utc = now
            alert.updated_at_utc = now
            db.commit()
            db.refresh(alert)
            if iot_publisher is not None:
                _publish_status_safe(db, alert, iot_publisher)
        return FallAlertIngestResponse(
            status="ok",
            alert_id=alert.id,
            event_id=alert.event_id,
            state=alert.state,
            duplicate=False,
        )

    # Action == CANCEL
    alert = (
        db.query(FallAlert)
        .filter(FallAlert.user_id == user.id, FallAlert.event_id == payload.event_id)
        .one_or_none()
    )
    if alert is None:
        return FallAlertIngestResponse(
            status="ok",
            alert_id=None,
            event_id=payload.event_id,
            state=FallAlertState.CANCELLED.value,
            duplicate=False,
        )

    if alert.state in ACTIVE_ALERT_STATES:
        reason = (
            payload.cancel_reason.value
            if payload.cancel_reason is not None
            else FallAlertCancelReason.user_cancel.value
        )
        alert.state = FallAlertState.CANCELLED.value
        alert.cancel_reason = reason
        alert.closed_at_utc = now
        alert.next_action_at_utc = None
        alert.updated_at_utc = now
        db.commit()
        db.refresh(alert)
        _publish_status_safe(db, alert, iot_publisher)
        return FallAlertIngestResponse(
            status="ok",
            alert_id=alert.id,
            event_id=alert.event_id,
            state=alert.state,
            duplicate=False,
        )

    return FallAlertIngestResponse(
        status="ok",
        alert_id=alert.id,
        event_id=alert.event_id,
        state=alert.state,
        duplicate=True,
    )


def process_due_fall_alerts(
    db: Session,
    *,
    now_utc: datetime | None = None,
    email_channel: SmtpEmailChannel | None = None,
    iot_publisher: AWSIoTPublishService | None = None,
    config: Settings | None = None,
) -> int:
    """Process all PENDING and ESCALATING alerts whose next_action_at_utc has elapsed.

    Returns the number of due alerts processed in this tick.
    """
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
        query = query.with_for_update(skip_locked=True)

    due_alerts = query.all()
    processed_count = 0
    for alert in due_alerts:
        try:
            _start_or_advance_escalation(
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
    """Validate token and load the corresponding FallAlert and FallAlertNotification without side effects."""
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
    """Acknowledge a fall alert from a verified signed token.

    Returns (alert, notification, transitioned_to_acknowledged).
    """
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


# --- Emergency Contacts CRUD ---


def list_alert_contacts(db: Session, user_id: int) -> list[AlertContactResponse]:
    """Return user's emergency contacts ordered by priority ascending."""
    contacts = (
        db.query(AlertContact)
        .filter(AlertContact.user_id == user_id)
        .order_by(AlertContact.priority.asc(), AlertContact.id.asc())
        .all()
    )
    return [AlertContactResponse.model_validate(c) for c in contacts]


def create_alert_contact(
    db: Session,
    user_id: int,
    payload: AlertContactCreate,
    *,
    config: Settings | None = None,
) -> AlertContactResponse:
    """Create a new emergency contact at the end of the user's priority list."""
    cfg = config or get_settings()
    existing = (
        db.query(AlertContact)
        .filter(AlertContact.user_id == user_id)
        .order_by(AlertContact.priority.asc(), AlertContact.id.asc())
        .all()
    )
    if len(existing) >= cfg.fall_alert_max_contacts:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Maximum of {cfg.fall_alert_max_contacts} emergency contacts reached",
        )
    if any(c.email.lower() == payload.email.lower() for c in existing):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An emergency contact with this email already exists",
        )

    now = datetime.now(UTC)
    contact = AlertContact(
        user_id=user_id,
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=payload.email,
        phone=payload.phone,
        priority=len(existing) + 1,
        created_at=now,
        updated_at=now,
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return AlertContactResponse.model_validate(contact)


def reorder_alert_contacts(
    db: Session,
    user_id: int,
    payload: AlertContactsOrderPayload,
) -> list[AlertContactResponse]:
    """Reorder all emergency contacts for a user according to the provided contact_ids list."""
    existing = (
        db.query(AlertContact)
        .filter(AlertContact.user_id == user_id)
        .order_by(AlertContact.priority.asc(), AlertContact.id.asc())
        .all()
    )
    existing_by_id = {c.id: c for c in existing}
    if len(payload.contact_ids) != len(existing_by_id) or set(payload.contact_ids) != set(
        existing_by_id.keys()
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="contact_ids must contain the exact set of existing contact IDs for this user",
        )

    now = datetime.now(UTC)
    for rank, cid in enumerate(payload.contact_ids, start=1):
        contact = existing_by_id[cid]
        contact.priority = rank
        contact.updated_at = now

    db.commit()
    return list_alert_contacts(db, user_id)


def update_alert_contact(
    db: Session,
    user_id: int,
    contact_id: int,
    payload: AlertContactUpdate,
) -> AlertContactResponse:
    """Update first_name, last_name, email, and/or phone of an existing emergency contact."""
    contact = (
        db.query(AlertContact)
        .filter(AlertContact.id == contact_id, AlertContact.user_id == user_id)
        .one_or_none()
    )
    if contact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Emergency contact not found",
        )

    if payload.email is not None and payload.email.lower() != contact.email.lower():
        duplicate = (
            db.query(AlertContact)
            .filter(
                AlertContact.user_id == user_id,
                AlertContact.id != contact_id,
                func.lower(AlertContact.email) == payload.email.lower(),
            )
            .first()
        )
        if duplicate is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An emergency contact with this email already exists",
            )
        contact.email = payload.email

    if payload.first_name is not None:
        contact.first_name = payload.first_name
    if payload.last_name is not None:
        contact.last_name = payload.last_name
    if "phone" in payload.model_fields_set:
        contact.phone = payload.phone

    contact.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(contact)
    return AlertContactResponse.model_validate(contact)


def delete_alert_contact(db: Session, user_id: int, contact_id: int) -> None:
    """Delete an emergency contact and re-compact remaining priorities (1..N)."""
    contact = (
        db.query(AlertContact)
        .filter(AlertContact.id == contact_id, AlertContact.user_id == user_id)
        .one_or_none()
    )
    if contact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Emergency contact not found",
        )

    db.delete(contact)
    db.flush()

    remaining = (
        db.query(AlertContact)
        .filter(AlertContact.user_id == user_id)
        .order_by(AlertContact.priority.asc(), AlertContact.id.asc())
        .all()
    )
    now = datetime.now(UTC)
    for idx, item in enumerate(remaining, start=1):
        if item.priority != idx:
            item.priority = idx
            item.updated_at = now

    db.commit()


def send_test_email_to_contact(
    db: Session,
    user: User,
    contact_id: int,
    *,
    email_channel: SmtpEmailChannel | None = None,
    iot_publisher: AWSIoTPublishService | None = None,
    config: Settings | None = None,
) -> AlertContactTestEmailResponse:
    """Send a test email (and optional test SMS command via MQTT) to a specific emergency contact."""
    contact = (
        db.query(AlertContact)
        .filter(AlertContact.id == contact_id, AlertContact.user_id == user.id)
        .one_or_none()
    )
    if contact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Emergency contact not found",
        )

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
            sms_dispatched = bool(iot_publisher.publish_fall_alert_status(user.id, test_payload))
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


# --- Serialization & GDPR Pseudonymization ---


def update_sms_notification_status(
    db: Session,
    user_id: int,
    payload: FallAlertSmsStatusRequest,
    *,
    now_utc: datetime | None = None,
    iot_publisher: AWSIoTPublishService | None = None,
) -> FallAlertIngestResponse:
    """Update status of a fall alert notification from authenticated mobile user."""
    now = _ensure_utc(now_utc) or datetime.now(UTC)
    notif = (
        db.query(FallAlertNotification)
        .join(FallAlert, FallAlertNotification.alert_id == FallAlert.id)
        .filter(
            FallAlertNotification.id == payload.notification_id,
            FallAlert.user_id == user_id,
        )
        .one_or_none()
    )
    if notif is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Fall alert notification '{payload.notification_id}' not found",
        )
    alert = notif.alert
    if notif.status != FallAlertNotificationStatus.ACKNOWLEDGED.value:
        notif.status = payload.sms_status
        notif.error_message = payload.error_message
        if payload.sent_at:
            try:
                notif.sent_at_utc = datetime.fromtimestamp(payload.sent_at / 1000.0, tz=UTC)
            except Exception:
                notif.sent_at_utc = now
        else:
            notif.sent_at_utc = now
        alert.updated_at_utc = now
        db.commit()
        db.refresh(alert)
        if iot_publisher is not None:
            _publish_status_safe(db, alert, iot_publisher)
    return FallAlertIngestResponse(
        status="ok",
        alert_id=alert.id,
        event_id=alert.event_id,
        state=alert.state,
        duplicate=False,
    )


def to_fall_alert_summary(alert: FallAlert, current_user: User) -> FallAlertSummary:
    """Serialize a FallAlert ORM instance to FallAlertSummary with GDPR pseudonymization for third-party records."""
    is_own = alert.user_id == current_user.id
    pseudo = get_subject_pseudonym(alert.user_id)
    user_display_name = f"Patient {pseudo}"
    is_pseudo = not is_own

    ack_by: FallAlertAcknowledgedBy | None = None
    if alert.acknowledged_by_first_name:
        ack_last = (
            (alert.acknowledged_by_last_name or "")
            if is_own
            else _mask_last_name(alert.acknowledged_by_last_name)
        )
        ack_by = FallAlertAcknowledgedBy(
            first_name=alert.acknowledged_by_first_name,
            last_name=ack_last,
        )

    notifications = alert.notifications or []
    notified_count = len(
        {
            n.rank
            for n in notifications
            if n.status
            in (
                FallAlertNotificationStatus.SENT.value,
                FallAlertNotificationStatus.PENDING_SMS.value,
                FallAlertNotificationStatus.ACKNOWLEDGED.value,
            )
        }
    )

    loc: FallAlertLocationResponse | None = None
    if alert.lat is not None and alert.lon is not None:
        loc = FallAlertLocationResponse(
            lat=alert.lat,
            lon=alert.lon,
            accuracy_m=alert.accuracy_m,
            captured_at=alert.location_captured_at_utc,
        )

    return FallAlertSummary(
        id=alert.id,
        event_id=alert.event_id,
        user_id=alert.user_id,
        user_display_name=user_display_name,
        subject_pseudonym=pseudo,
        is_pseudonymized=is_pseudo,
        device_id=alert.device_id,
        event_type=alert.event_type,
        confidence=alert.confidence_score,
        state=alert.state,
        cancel_reason=alert.cancel_reason,
        delivery=alert.delivery,
        has_location=(alert.lat is not None and alert.lon is not None),
        location=loc,
        detected_at=alert.detected_at_utc,
        escalation_started_at=alert.escalation_started_at_utc,
        closed_at=alert.closed_at_utc,
        acknowledged_by=ack_by,
        notified_contacts_count=notified_count,
    )


def to_fall_alert_detail(alert: FallAlert, current_user: User) -> FallAlertDetail:
    """Serialize a FallAlert ORM instance to FallAlertDetail with notifications and GPS coordinates."""
    summary = to_fall_alert_summary(alert, current_user)
    is_own = alert.user_id == current_user.id

    location: FallAlertLocationResponse | None = None
    if alert.lat is not None and alert.lon is not None:
        location = FallAlertLocationResponse(
            lat=alert.lat,
            lon=alert.lon,
            accuracy_m=alert.accuracy_m,
            captured_at=alert.location_captured_at_utc,
        )

    notif_items: list[FallAlertNotificationResponse] = []
    for n in alert.notifications or []:
        if is_own:
            c_last = n.contact_last_name
            c_email = n.contact_email
            c_phone = n.contact_phone
        else:
            c_last = _mask_last_name(n.contact_last_name)
            c_email = mask_email(n.contact_email) or "***"
            c_phone = _mask_phone(n.contact_phone)

        channel_val = "sms" if getattr(n, "channel", "email") == "sms" else "email"
        notif_items.append(
            FallAlertNotificationResponse(
                id=n.id,
                rank=n.rank,
                channel=channel_val,  # type: ignore[arg-type]
                contact_first_name=n.contact_first_name,
                contact_last_name=c_last,
                contact_email=c_email,
                contact_phone=c_phone,
                status=n.status,
                sent_at=n.sent_at_utc,
                acknowledged_at=n.acknowledged_at_utc,
                error_message=n.error_message,
            )
        )

    return FallAlertDetail(
        **summary.model_dump(),
        notifications=notif_items,
    )
