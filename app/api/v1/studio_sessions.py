import logging
from datetime import datetime

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import RequireClinicianOrAdmin
from app.api.v1.utils import validate_and_normalize_date_range
from app.db.database import get_db
from app.schemas.studio import (
    PaginatedSessionsResponse,
    StudioAuthorSummary,
    StudioSessionDetail,
    StudioSessionSummary,
    StudioSessionTrimPayload,
    StudioSessionUpdatePayload,
)
from app.schemas.telemetry import StudioSessionReadingsResponse
from app.services.studio_service import (
    delete_studio_session,
    get_authorized_session,
    query_studio_authors,
    query_studio_sessions,
    to_session_summary,
    trim_studio_session,
    update_studio_session,
)
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
        cursor: str | None = Query(default=None, description="Opaque cursor for keyset pagination"),
        label: str | None = Query(default=None, description="Single activity label or comma-separated list of labels"),
        device_id: str | None = Query(default=None, description="Single device ID or comma-separated list of device IDs"),
        pseudonym: str | None = Query(default=None, description="Filter by author pseudonym (admin)"),
        is_validated: bool | None = Query(default=None, description="Filter by session validation status"),
        is_hitl: bool | None = Query(default=None, description="Filter by HITL session (True) or standard (False)"),
        classification: str | None = Query(default=None, description="Filter by classification ('edge_ai' or 'research')"),
        start_date: datetime | None = Query(None, description="Start date/time (inclusive, ISO 8601)"),
        end_date: datetime | None = Query(None, description="End date/time (inclusive, ISO 8601)"),
        sample_count_min: int | None = Query(default=None, ge=0, description="Minimum number of IMU samples"),
        sample_count_max: int | None = Query(default=None, ge=0, description="Maximum number of IMU samples"),
        duration_sec_min: float | None = Query(default=None, ge=0, description="Minimum session duration in seconds"),
        duration_sec_max: float | None = Query(default=None, ge=0, description="Maximum session duration in seconds"),
        db: Session = Depends(get_db),
        request: Request = None,  # type: ignore[assignment]
    ) -> PaginatedSessionsResponse:
        """List studio sessions with RBAC, cursor keyset pagination, and multi-value filters."""
        raw_end = request.query_params.get("end_date") if request is not None else None
        norm_start, norm_end = validate_and_normalize_date_range(start_date, end_date, raw_end)

        return query_studio_sessions(
            db,
            user,
            page=page,
            size=size,
            cursor=cursor,
            label=label,
            device_id=device_id,
            pseudonym=pseudonym,
            is_validated=is_validated,
            is_hitl=is_hitl,
            classification=classification,
            start_date=norm_start,
            end_date=norm_end,
            sample_count_min=sample_count_min,
            sample_count_max=sample_count_max,
            duration_sec_min=duration_sec_min,
            duration_sec_max=duration_sec_max,
        )

    @router.get("/authors", response_model=list[StudioAuthorSummary])
    def list_studio_authors(
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> list[StudioAuthorSummary]:
        """List authors who have recorded studio sessions with GDPR pseudonymization for third-party authors."""
        return query_studio_authors(db, user)

    @router.get("/{session_id}", response_model=StudioSessionDetail)
    def get_session_detail(
        session_id: str,
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> StudioSessionDetail:
        """Fetch studio session metadata from Aurora DB (including sample_count)."""
        session = get_authorized_session(db, session_id, user)
        summary = to_session_summary(session, is_admin=user.is_admin, current_user_id=user.id)
        return StudioSessionDetail.model_validate(summary.model_dump())

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
                session_id=str(session.uuid),
            )
        except (BotoCoreError, ClientError) as error:
            logger.error("DynamoDB error querying readings for session %s: %s", session.uuid, error)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to retrieve studio session readings from telemetry store",
            )

        if readings is None:
            return StudioSessionReadingsResponse(
                device_id=session.device_id,
                session_id=str(session.uuid),
                label=session.label,
                sample_count=0,
                readings=[],
            )

        return readings

    @router.patch("/{session_id}/trim", response_model=StudioSessionSummary)
    def trim_session(
        session_id: str,
        payload: StudioSessionTrimPayload,
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> StudioSessionSummary:
        """Keep a selected time interval from an unvalidated session."""
        session = get_authorized_session(db, session_id, user)
        updated = trim_studio_session(db, session, payload, telemetry_service)
        return to_session_summary(updated, is_admin=user.is_admin, current_user_id=user.id)

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
        return to_session_summary(session, is_admin=user.is_admin, current_user_id=user.id)

    @router.patch("/{session_id}", response_model=StudioSessionSummary)
    def update_session(
        session_id: str,
        payload: StudioSessionUpdatePayload,
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> StudioSessionSummary:
        """Reclassify a studio session's activity label or update validation in PostgreSQL and DynamoDB."""
        session = get_authorized_session(db, session_id, user)
        updated = update_studio_session(db, session, payload, telemetry_service)
        return to_session_summary(updated, is_admin=user.is_admin, current_user_id=user.id)

    @router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_session(
        session_id: str,
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> Response:
        """Purge a studio session from PostgreSQL and delete all raw IMU frames in DynamoDB."""
        session = get_authorized_session(db, session_id, user)
        delete_studio_session(db, session, telemetry_service)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
