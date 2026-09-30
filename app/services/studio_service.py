"""Studio session management, authorization, and statistics computation service."""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import StudioSession, User
from app.schemas.studio import StudioSessionSummary
from app.schemas.telemetry import StudioDatasetStatsResponse


def parse_session_uuid(session_id: str | UUID) -> UUID:
    """Safely parse a studio session UUID or raise 404."""
    if isinstance(session_id, UUID):
        return session_id
    try:
        return UUID(str(session_id))
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Studio session '{session_id}' not found",
        )


def get_authorized_session(
    db: Session,
    session_id: str | UUID,
    user: User,
) -> StudioSession:
    """Retrieve a studio session ensuring role-based access control.

    - Requires clinician or admin role.
    - Clinicians can only access their own sessions.
    - Admins can access all sessions.
    """
    if not user.is_clinician_or_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: access to studio sessions requires clinician or admin privileges",
        )

    sess_uuid = parse_session_uuid(session_id)
    session = db.query(StudioSession).filter(StudioSession.id == sess_uuid).one_or_none()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Studio session '{session_id}' not found",
        )

    if not user.is_admin and session.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: cannot access or modify another user's studio session",
        )

    return session


def to_session_summary(session: StudioSession, is_admin: bool) -> StudioSessionSummary:
    """Serialize a StudioSession ORM model into a StudioSessionSummary schema."""
    return StudioSessionSummary(
        id=session.id,
        device_id=session.device_id,
        user_id=session.user_id,
        user_email=session.user.email if (is_admin and session.user) else None,
        label=session.label,
        sample_count=session.sample_count or 0,
        duration_sec=session.duration_sec or 5.0,
        is_validated=bool(session.is_validated),
        created_at=session.created_at,
    )


def compute_studio_stats(
    db: Session,
    user: User,
    device_id: str | None = None,
) -> StudioDatasetStatsResponse:
    """Compute studio dataset metrics from PostgreSQL (Aurora).

    - Non-admins only see aggregate stats for their own sessions.
    - Admins see global aggregates or device aggregates across all users.
    """
    query = db.query(StudioSession)

    if not user.is_admin:
        query = query.filter(StudioSession.user_id == user.id)

    if device_id is not None:
        query = query.filter(StudioSession.device_id == device_id)

    total_sessions = query.count()
    total_duration = (
        query.with_entities(func.coalesce(func.sum(StudioSession.duration_sec), 0.0)).scalar()
        or 0.0
    )
    total_samples = (
        query.with_entities(func.coalesce(func.sum(StudioSession.sample_count), 0)).scalar()
        or 0
    )

    label_rows = (
        query.with_entities(StudioSession.label, func.count(StudioSession.id))
        .group_by(StudioSession.label)
        .all()
    )
    by_label = {str(lbl): int(cnt) for lbl, cnt in label_rows}

    return StudioDatasetStatsResponse(
        device_id=device_id,
        total_sessions=total_sessions,
        total_duration_sec=round(float(total_duration), 2),
        total_samples=int(total_samples),
        by_label=by_label,
    )

