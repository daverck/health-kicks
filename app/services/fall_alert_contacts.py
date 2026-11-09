"""CRUD and operational services for user emergency alert contacts."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import AlertContact, User
from app.schemas.fall_alert import (
    AlertContactCreate,
    AlertContactResponse,
    AlertContactsOrderPayload,
    AlertContactTestEmailResponse,
    AlertContactUpdate,
)
from app.services.aws_iot_service import AWSIoTPublishService
from app.services.email_service import SmtpEmailChannel
from app.services.fall_alert_notifier import dispatch_test_email_and_sms

logger = logging.getLogger("healthkicks.fall_alert.contacts")


def get_user_contact(
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
    contact_id: int | str | uuid.UUID,
    payload: AlertContactUpdate,
) -> AlertContactResponse:
    """Update first_name, last_name, email, and/or phone of an existing emergency contact."""
    contact = get_user_contact(db, user_id, contact_id)
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
    contact = get_user_contact(db, user_id, contact_id)
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
    contact = get_user_contact(db, user.id, contact_id)
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
