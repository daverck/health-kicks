"""Service for recording and querying tamper-resistant access audit logs."""

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import Request
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.db.models import AccessAuditLog, User, UserRole
from app.schemas.audit import (
    AccessAuditLogResponse,
    AuditAction,
    AuditResource,
    PaginatedAccessAuditLogsResponse,
)

logger = logging.getLogger(__name__)


def extract_client_metadata(request: Request | None) -> tuple[str | None, str | None]:
    """Extract client IP address and User-Agent header from incoming request."""
    if request is None:
        return None, None

    # Handle standard X-Forwarded-For or client host
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        ip_address = x_forwarded_for.split(",")[0].strip()
    elif request.client and request.client.host:
        ip_address = request.client.host
    else:
        ip_address = None

    user_agent = request.headers.get("user-agent")
    if user_agent and len(user_agent) > 255:
        user_agent = user_agent[:255]

    return ip_address, user_agent


def record_access_log(
    db: Session,
    actor: User,
    patient: User | UUID | str,
    action: AuditAction | str,
    resource: AuditResource | str,
    patient_pseudonym: str | None = None,
    resource_id: str | None = None,
    request: Request | None = None,
) -> AccessAuditLog | None:
    """Record an audit entry when a trainer or admin accesses patient records.

    Self-access by patients to their own data is excluded to avoid administrative audit pollution.
    """
    # 1. Resolve patient UUID and pseudonym
    if isinstance(patient, User):
        patient_uuid = patient.uuid
        resolved_pseudonym = patient.pseudonym
    elif isinstance(patient, UUID):
        patient_uuid = patient
        resolved_pseudonym = patient_pseudonym
    else:
        try:
            patient_uuid = UUID(str(patient))
            resolved_pseudonym = patient_pseudonym
        except ValueError:
            # Fallback if string is not a valid UUID (e.g. integer or pseudonym lookup)
            logger.warning("Could not parse patient UUID for audit logging: %s", patient)
            return None

    # 2. Skip self-access (only administrative / clinical access is audited)
    if actor.uuid == patient_uuid:
        return None

    # 3. Only audit administrative or clinical roles (trainer or admin)
    if actor.role not in (UserRole.admin, UserRole.trainer):
        return None

    action_str = action.value if hasattr(action, "value") else str(action)
    resource_str = resource.value if hasattr(resource, "value") else str(resource)
    actor_role_str = actor.role.value if hasattr(actor.role, "value") else str(actor.role)

    ip_address, user_agent = extract_client_metadata(request)

    entry = AccessAuditLog(
        uuid=uuid4(),
        actor_uuid=actor.uuid,
        actor_role=actor_role_str,
        actor_email=actor.email,
        patient_uuid=patient_uuid,
        patient_pseudonym=resolved_pseudonym,
        action=action_str,
        resource=resource_str,
        resource_id=str(resource_id) if resource_id is not None else None,
        ip_address=ip_address,
        user_agent=user_agent,
        created_at_utc=datetime.now(UTC),
    )

    db.add(entry)
    db.commit()
    db.refresh(entry)

    logger.info(
        "Access audit logged: actor=%s role=%s patient=%s action=%s resource=%s",
        actor.uuid,
        actor_role_str,
        patient_uuid,
        action_str,
        resource_str,
    )
    return entry


def query_access_logs(
    db: Session,
    page: int = 1,
    page_size: int = 50,
    actor_uuid: UUID | None = None,
    patient_uuid: UUID | None = None,
    action: str | None = None,
    resource: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> PaginatedAccessAuditLogsResponse:
    """Query immutable access audit logs with filtering and pagination."""
    query = db.query(AccessAuditLog)

    if actor_uuid is not None:
        query = query.filter(AccessAuditLog.actor_uuid == actor_uuid)
    if patient_uuid is not None:
        query = query.filter(AccessAuditLog.patient_uuid == patient_uuid)
    if action is not None:
        query = query.filter(AccessAuditLog.action == action.upper())
    if resource is not None:
        query = query.filter(AccessAuditLog.resource == resource.lower())
    if from_date is not None:
        query = query.filter(AccessAuditLog.created_at_utc >= from_date)
    if to_date is not None:
        query = query.filter(AccessAuditLog.created_at_utc <= to_date)

    total = query.count()

    offset = (page - 1) * page_size
    records = (
        query.order_by(desc(AccessAuditLog.created_at_utc)).offset(offset).limit(page_size).all()
    )

    items = [
        AccessAuditLogResponse(
            uuid=r.uuid,
            actor_uuid=r.actor_uuid,
            actor_role=r.actor_role,
            actor_email=r.actor_email,
            patient_uuid=r.patient_uuid,
            patient_pseudonym=r.patient_pseudonym,
            action=r.action,
            resource=r.resource,
            resource_id=r.resource_id,
            ip_address=r.ip_address,
            user_agent=r.user_agent,
            created_at_utc=r.created_at_utc,
        )
        for r in records
    ]

    return PaginatedAccessAuditLogsResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
    )
