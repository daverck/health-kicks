"""Business logic and state machine for fall alerting, emergency contacts, SMTP email, and mobile SMS escalation."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
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
    FallAlertNotificationResponse,
    FallAlertSmsStatusRequest,
    FallAlertSummary,
)
from app.services.aws_iot_service import AWSIoTPublishService
from app.services.email_service import SmtpEmailChannel
from app.services.fall_alert_notifier import dispatch_test_email_and_sms
from app.services.fall_alert_resolution import (
    _apply_location_to_alert,
    _publish_status_safe,
    build_downlink_status_payload,
    resolve_user_for_ingest,
)
from app.services.fall_alert_tokens import (
    ACK_TOKEN_SALT,
    AckTokenError,
    _ensure_utc,
    _mask_last_name,
    _mask_phone,
    build_ack_url,
    generate_ack_token,
    verify_ack_token,
)
from app.services.fall_alert_workflow import (
    ACTIVE_ALERT_STATES,
    acknowledge_fall_alert,
    get_alert_and_notification_for_token,
    process_due_fall_alerts,
    start_or_advance_escalation,
)

__all__ = [
    "ACK_TOKEN_SALT",
    "ACTIVE_ALERT_STATES",
    "AckTokenError",
    "acknowledge_fall_alert",
    "build_ack_url",
    "build_downlink_status_payload",
    "create_alert_contact",
    "delete_alert_contact",
    "generate_ack_token",
    "get_alert_and_notification_for_token",
    "ingest_fall_alert_event",
    "list_alert_contacts",
    "process_due_fall_alerts",
    "reorder_alert_contacts",
    "resolve_user_for_ingest",
    "send_test_email_to_contact",
    "to_fall_alert_detail",
    "to_fall_alert_summary",
    "update_alert_contact",
    "update_sms_notification_status",
    "verify_ack_token",
]

logger = logging.getLogger("healthkicks.fall_alert")


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
            start_or_advance_escalation(
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


def _get_user_contact(
    db: Session,
    user_id: int,
    contact_identifier: int | str | uuid.UUID,
) -> AlertContact | None:
    """Resolve an AlertContact for a user by int id or UUID."""
    if isinstance(contact_identifier, uuid.UUID):
        return (
            db.query(AlertContact)
            .filter(AlertContact.uuid == contact_identifier, AlertContact.user_id == user_id)
            .one_or_none()
        )
    if isinstance(contact_identifier, int):
        return (
            db.query(AlertContact)
            .filter(AlertContact.id == contact_identifier, AlertContact.user_id == user_id)
            .one_or_none()
        )
    raw = str(contact_identifier).strip()
    try:
        parsed_uuid = uuid.UUID(raw)
        return (
            db.query(AlertContact)
            .filter(AlertContact.uuid == parsed_uuid, AlertContact.user_id == user_id)
            .one_or_none()
        )
    except ValueError:
        pass
    if raw.isdigit():
        return (
            db.query(AlertContact)
            .filter(AlertContact.id == int(raw), AlertContact.user_id == user_id)
            .one_or_none()
        )
    return None


def update_alert_contact(
    db: Session,
    user_id: int,
    contact_id: int | str | uuid.UUID,
    payload: AlertContactUpdate,
) -> AlertContactResponse:
    """Update first_name, last_name, email, and/or phone of an existing emergency contact."""
    contact = _get_user_contact(db, user_id, contact_id)
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
                AlertContact.id != contact.id,
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


def delete_alert_contact(db: Session, user_id: int, contact_id: int | str | uuid.UUID) -> None:
    """Delete an emergency contact and re-compact remaining priorities (1..N)."""
    contact = _get_user_contact(db, user_id, contact_id)
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
    contact_id: int | str | uuid.UUID,
    *,
    email_channel: SmtpEmailChannel | None = None,
    iot_publisher: AWSIoTPublishService | None = None,
    config: Settings | None = None,
) -> AlertContactTestEmailResponse:
    """Send a test email (and optional test SMS command via MQTT) to a specific emergency contact."""
    contact = _get_user_contact(db, user.id, contact_id)
    if contact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Emergency contact not found",
        )

    return dispatch_test_email_and_sms(
        db,
        user,
        contact,
        email_channel=email_channel,
        iot_publisher=iot_publisher,
        config=config,
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
    pseudo = (
        getattr(alert.user, "pseudonym", None)
        if alert.user and getattr(alert.user, "pseudonym", None)
        else get_subject_pseudonym(alert.user_id)
    )
    user_display_name = f"Patient {pseudo}"
    is_pseudo = not is_own
    user_uuid = getattr(alert.user, "uuid", None) if alert.user else None
    alert_uuid = getattr(alert, "uuid", None)

    notifications = alert.notifications or []

    ack_by: FallAlertAcknowledgedBy | None = None
    if alert.acknowledged_by_first_name:
        ack_last = (
            (alert.acknowledged_by_last_name or "")
            if is_own
            else _mask_last_name(alert.acknowledged_by_last_name)
        )
        ack_rank: int | None = None
        for n in notifications:
            if n.status == FallAlertNotificationStatus.ACKNOWLEDGED.value:
                ack_rank = n.rank
                break
        if ack_rank is None and alert.acknowledged_by_contact_id:
            for n in notifications:
                if n.contact_id == alert.acknowledged_by_contact_id:
                    ack_rank = n.rank
                    break
        ack_by = FallAlertAcknowledgedBy(
            first_name=alert.acknowledged_by_first_name,
            last_name=ack_last,
            rank=ack_rank,
        )
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
        uuid=alert_uuid,
        event_id=alert.event_id,
        user_id=alert.user_id,
        user_uuid=user_uuid,
        user_pseudonym=pseudo,
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
