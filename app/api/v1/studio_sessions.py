"""FastAPI router for Studio sessions history, curation, and IMU inspection."""

import logging
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.db.database import get_db
from app.db.models import StudioSession, User, UserRole
from app.schemas.studio import (
    PaginatedSessionsResponse,
    StudioSessionSummary,
    StudioSessionUpdatePayload,
)
from app.schemas.telemetry import StudioSessionReadingsResponse
from app.services.telemetry_service import TelemetryService

logger = logging.getLogger(__name__)


def _is_admin(user: User) -> bool:
    return user.role == UserRole.admin


def _parse_uuid(session_id: str) -> UUID:
    try:
        return UUID(str(session_id))
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Studio session '{session_id}' not found",
        )


def _get_authorized_session(
    session_id: str,
    user: User,
    db: Session,
) -> StudioSession:
    sess_uuid = _parse_uuid(session_id)
    session = db.query(StudioSession).filter(StudioSession.id == sess_uuid).one_or_none()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Studio session '{session_id}' not found",
        )
    if not _is_admin(user) and session.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: access to this studio session is denied",
        )
    return session


def _to_summary(session: StudioSession, is_admin: bool) -> StudioSessionSummary:
    return StudioSessionSummary(
        id=session.id,
        device_id=session.device_id,
        user_id=session.user_id,
        user_email=session.user.email if (is_admin and session.user) else None,
        label=session.label,
        sample_count=session.sample_count,
        duration_sec=session.duration_sec,
        created_at=session.created_at,
    )


def create_studio_sessions_router(
    service: TelemetryService | None = None,
) -> APIRouter:
    """Build the Studio sessions router with injected TelemetryService."""
    router = APIRouter(prefix="/api/v1/studio/sessions", tags=["Studio Sessions"])
    telemetry_service = service or TelemetryService()

    @router.get("", response_model=PaginatedSessionsResponse)
    def list_sessions(
        user: CurrentUser,
        page: int = Query(default=1, ge=1),
        size: int = Query(default=20, ge=1, le=100),
        label: str | None = Query(default=None),
        device_id: str | None = Query(default=None),
        user_id: int | None = Query(default=None),
        db: Session = Depends(get_db),
    ) -> PaginatedSessionsResponse:
        """List studio sessions with RBAC, pagination, and filters."""
        query = db.query(StudioSession)

        if not _is_admin(user):
            query = query.filter(StudioSession.user_id == user.id)
        else:
            if user_id is not None:
                query = query.filter(StudioSession.user_id == user_id)

        if label is not None:
            query = query.filter(StudioSession.label == label)

        if device_id is not None:
            query = query.filter(StudioSession.device_id == device_id)

        total = query.count()
        sessions = (
            query.order_by(StudioSession.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
            .all()
        )

        items = [_to_summary(s, is_admin=_is_admin(user)) for s in sessions]
        return PaginatedSessionsResponse(
            items=items,
            total=total,
            page=page,
            size=size,
        )

    @router.get("/{session_id}/readings", response_model=StudioSessionReadingsResponse)
    def get_session_readings(
        session_id: str,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> StudioSessionReadingsResponse:
        """Fetch raw IMU readings for an authorized studio session from DynamoDB."""
        session = _get_authorized_session(session_id, user, db)
        try:
            readings = telemetry_service.get_session_readings(
                device_id=session.device_id,
                session_id=str(session.id),
            )
        except (BotoCoreError, ClientError) as error:
            logger.error("DynamoDB error querying readings for session %s: %s", session.id, error)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to retrieve studio session readings from telemetry store",
            )

        if readings is None:
            return StudioSessionReadingsResponse(
                device_id=session.device_id,
                session_id=str(session.id),
                label=session.label,
                sample_count=0,
                readings=[],
            )

        return readings

    @router.patch("/{session_id}", response_model=StudioSessionSummary)
    def update_session(
        session_id: str,
        payload: StudioSessionUpdatePayload,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> StudioSessionSummary:
        """Reclassify a studio session's activity label in PostgreSQL and DynamoDB."""
        session = _get_authorized_session(session_id, user, db)

        session.label = payload.label
        db.commit()
        db.refresh(session)

        try:
            telemetry_service.update_session_label(
                device_id=session.device_id,
                session_id=str(session.id),
                new_label=payload.label,
            )
        except (BotoCoreError, ClientError) as error:
            logger.warning(
                "Could not propagate label update to DynamoDB for session %s: %s",
                session.id,
                error,
            )

        return _to_summary(session, is_admin=_is_admin(user))

    @router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_session(
        session_id: str,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> Response:
        """Purge a studio session from PostgreSQL and delete all raw IMU frames in DynamoDB."""
        session = _get_authorized_session(session_id, user, db)

        try:
            telemetry_service.delete_session_readings(
                device_id=session.device_id,
                session_id=str(session.id),
            )
        except (BotoCoreError, ClientError) as error:
            logger.warning(
                "Could not purge readings from DynamoDB for session %s: %s",
                session.id,
                error,
            )

        db.delete(session)
        db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
