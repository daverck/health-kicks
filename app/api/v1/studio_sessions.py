"""FastAPI router for Studio sessions history, curation, and IMU inspection."""

import logging
from datetime import datetime

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import RequireClinicianOrAdmin
from app.api.v1.utils import validate_and_normalize_date_range
from app.db.database import get_db
from app.db.models import StudioSession, User, UserRole
from app.schemas.studio import (
    PaginatedSessionsResponse,
    StudioAuthorSummary,
    StudioSessionDetail,
    StudioSessionSummary,
    StudioSessionUpdatePayload,
)
from app.schemas.telemetry import StudioSessionReadingsResponse
from app.services.studio_service import get_authorized_session, to_session_summary
from app.services.telemetry_service import TelemetryService

logger = logging.getLogger(__name__)


def create_studio_sessions_router(
    service: TelemetryService | None = None,
) -> APIRouter:
    """Build the Studio sessions router with injected TelemetryService."""
    router = APIRouter(prefix="/api/v1/studio/sessions", tags=["Studio Sessions"])
    telemetry_service = service or TelemetryService()

    @router.get("", response_model=PaginatedSessionsResponse)
    def list_sessions(
        user: RequireClinicianOrAdmin,
        page: int = Query(default=1, ge=1),
        size: int = Query(default=20, ge=1, le=100),
        label: str | None = Query(default=None, description="Single activity label or comma-separated list of labels"),
        device_id: str | None = Query(default=None, description="Single device ID or comma-separated list of device IDs"),
        user_id: str | None = Query(default=None, description="Single user ID or comma-separated list of user IDs"),
        is_validated: bool | None = Query(default=None, description="Filtrer par statut de validation de la session"),
        start_date: datetime | None = Query(None, description="Date/heure de début (inclusive, ISO 8601)"),
        end_date: datetime | None = Query(None, description="Date/heure de fin (inclusive, ISO 8601)"),
        db: Session = Depends(get_db),
        request: Request = None,
    ) -> PaginatedSessionsResponse:
        """List studio sessions with RBAC, pagination, and multi-value filters."""
        raw_end = request.query_params.get("end_date") if request is not None else None
        norm_start, norm_end = validate_and_normalize_date_range(start_date, end_date, raw_end)

        query = db.query(StudioSession)

        if user.role != UserRole.admin:
            query = query.filter(StudioSession.user_id == user.id)
        elif user_id is not None:
            raw_uids = [u.strip() for u in str(user_id).split(",") if u.strip()]
            if raw_uids and not any(u.lower() == "all" for u in raw_uids):
                target_uids = [int(u) for u in raw_uids if u.isdigit()]
                if target_uids:
                    query = query.filter(StudioSession.user_id.in_(target_uids))
                else:
                    query = query.filter(StudioSession.user_id == -1)

        if label is not None:
            target_labels = [l.strip() for l in label.split(",") if l.strip()]
            if target_labels and not any(l.lower() == "all" for l in target_labels):
                query = query.filter(StudioSession.label.in_(target_labels))

        if device_id is not None:
            target_devs = [d.strip() for d in device_id.split(",") if d.strip()]
            if target_devs and not any(d.lower() == "all" for d in target_devs):
                query = query.filter(StudioSession.device_id.in_(target_devs))

        if is_validated is not None and isinstance(is_validated, bool):
            query = query.filter(StudioSession.is_validated == is_validated)

        if norm_start is not None:
            query = query.filter(StudioSession.created_at >= norm_start)

        if norm_end is not None:
            query = query.filter(StudioSession.created_at <= norm_end)

        total = query.count()
        sessions = (
            query.order_by(StudioSession.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
            .all()
        )

        items = [to_session_summary(s, is_admin=user.is_admin) for s in sessions]
        return PaginatedSessionsResponse(
            items=items,
            total=total,
            page=page,
            size=size,
        )

    @router.get("/authors", response_model=list[StudioAuthorSummary])
    def list_studio_authors(
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> list[StudioAuthorSummary]:
        """List authors who have recorded studio sessions."""
        if user.role == UserRole.admin:
            users = (
                db.query(User.id, User.email, User.name)
                .join(StudioSession, StudioSession.user_id == User.id)
                .distinct()
                .order_by(User.email)
                .all()
            )
            return [StudioAuthorSummary(id=u.id, email=u.email, name=u.name) for u in users]
        return [StudioAuthorSummary(id=user.id, email=user.email, name=user.name)]

    @router.get("/{session_id}", response_model=StudioSessionDetail)
    def get_session_detail(
        session_id: str,
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> StudioSessionDetail:
        """Fetch studio session metadata from Aurora DB (including sample_count)."""
        session = get_authorized_session(db, session_id, user)
        return to_session_summary(session, is_admin=user.is_admin)

    @router.get("/{session_id}/readings", response_model=StudioSessionReadingsResponse)
    def get_session_readings(
        session_id: str,
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> StudioSessionReadingsResponse:
        """Fetch raw IMU readings for an authorized studio session from DynamoDB."""
        session = get_authorized_session(db, session_id, user)
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

    @router.patch("/{session_id}/confirm", response_model=StudioSessionSummary)
    def confirm_session(
        session_id: str,
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> StudioSessionSummary:
        """Confirm and validate a studio session for dataset inclusion."""
        session = get_authorized_session(db, session_id, user)
        session.is_validated = True
        db.commit()
        db.refresh(session)
        return to_session_summary(session, is_admin=user.is_admin)

    @router.patch("/{session_id}", response_model=StudioSessionSummary)
    def update_session(
        session_id: str,
        payload: StudioSessionUpdatePayload,
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> StudioSessionSummary:
        """Reclassify a studio session's activity label or update validation in PostgreSQL and DynamoDB."""
        session = get_authorized_session(db, session_id, user)

        if payload.label is not None:
            session.label = payload.label
        if payload.is_validated is not None:
            session.is_validated = payload.is_validated

        db.commit()
        db.refresh(session)

        if payload.label is not None:
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

        return to_session_summary(session, is_admin=user.is_admin)

    @router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_session(
        session_id: str,
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> Response:
        """Purge a studio session from PostgreSQL and delete all raw IMU frames in DynamoDB."""
        session = get_authorized_session(db, session_id, user)

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
