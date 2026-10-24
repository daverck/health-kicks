import base64
import logging
from datetime import datetime
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.api.deps import RequireClinicianOrAdmin
from app.api.v1.utils import validate_and_normalize_date_range
from app.core.pseudonymization import get_subject_pseudonym
from app.db.database import get_db
from app.db.models import StudioSession, User, UserRole
from app.schemas.studio import (
    PaginatedSessionsResponse,
    StudioAuthorSummary,
    StudioSessionDetail,
    StudioSessionSummary,
    StudioSessionTrimPayload,
    StudioSessionUpdatePayload,
)
from app.schemas.telemetry import StudioSessionReadingsResponse
from app.services.studio_service import get_authorized_session, to_session_summary
from app.services.telemetry_service import TelemetryService

logger = logging.getLogger(__name__)


def encode_cursor(created_at: datetime, session_id: UUID) -> str:
    """Encode created_at timestamp and session UUID into an opaque URL-safe base64 cursor."""
    raw = f"{created_at.isoformat()}|{str(session_id)}"
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii")


def decode_cursor(cursor_str: str) -> tuple[datetime, UUID] | None:
    """Safely decode an opaque cursor into (created_at, session_id) or return None."""
    try:
        raw = base64.urlsafe_b64decode(cursor_str.encode("ascii")).decode("ascii")
        ts_part, id_part = raw.split("|", 1)
        return datetime.fromisoformat(ts_part), UUID(id_part)
    except Exception:
        return None


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
        user_id: str | None = Query(default=None, description="Single user ID or comma-separated list of user IDs"),
        is_validated: bool | None = Query(default=None, description="Filtrer par statut de validation de la session"),
        is_hitl: bool | None = Query(default=None, description="Filtrer par session HITL (True) ou standard (False)"),
        start_date: datetime | None = Query(None, description="Date/heure de début (inclusive, ISO 8601)"),
        end_date: datetime | None = Query(None, description="Date/heure de fin (inclusive, ISO 8601)"),
        sample_count_min: int | None = Query(default=None, ge=0, description="Minimum number of IMU samples"),
        sample_count_max: int | None = Query(default=None, ge=0, description="Maximum number of IMU samples"),
        duration_sec_min: float | None = Query(default=None, ge=0, description="Minimum session duration in seconds"),
        duration_sec_max: float | None = Query(default=None, ge=0, description="Maximum session duration in seconds"),
        db: Session = Depends(get_db),
        request: Request = None,  # type: ignore[assignment]
    ) -> PaginatedSessionsResponse:
        """List studio sessions with RBAC, cursor keyset pagination, and multi-value filters."""
        if sample_count_min is not None and sample_count_max is not None and sample_count_min > sample_count_max:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="sample_count_min must be less than or equal to sample_count_max",
            )
        if duration_sec_min is not None and duration_sec_max is not None and duration_sec_min > duration_sec_max:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="duration_sec_min must be less than or equal to duration_sec_max",
            )

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
            target_labels = [lbl.strip() for lbl in label.split(",") if lbl.strip()]
            if target_labels and not any(lbl.lower() == "all" for lbl in target_labels):
                query = query.filter(StudioSession.label.in_(target_labels))

        if device_id is not None:
            target_devs = [d.strip() for d in device_id.split(",") if d.strip()]
            if target_devs and not any(d.lower() == "all" for d in target_devs):
                query = query.filter(StudioSession.device_id.in_(target_devs))

        if is_validated is not None and isinstance(is_validated, bool):
            query = query.filter(StudioSession.is_validated == is_validated)

        if is_hitl is not None and isinstance(is_hitl, bool):
            query = query.filter(StudioSession.is_hitl == is_hitl)

        if norm_start is not None:
            query = query.filter(StudioSession.created_at >= norm_start)

        if norm_end is not None:
            query = query.filter(StudioSession.created_at <= norm_end)

        if sample_count_min is not None:
            query = query.filter(StudioSession.sample_count >= sample_count_min)
        if sample_count_max is not None:
            query = query.filter(StudioSession.sample_count <= sample_count_max)
        if duration_sec_min is not None:
            query = query.filter(StudioSession.duration_sec >= duration_sec_min)
        if duration_sec_max is not None:
            query = query.filter(StudioSession.duration_sec <= duration_sec_max)

        total = query.count()
        next_cursor: str | None = None

        if cursor is not None:
            decoded = decode_cursor(cursor)
            if decoded is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid pagination cursor format",
                )
            cursor_dt, cursor_id = decoded
            query = query.filter(
                or_(
                    StudioSession.created_at < cursor_dt,
                    and_(
                        StudioSession.created_at == cursor_dt,
                        StudioSession.id < cursor_id,
                    ),
                )
            )

        # Unified keyset pagination on (created_at DESC, id DESC) without slow SQL OFFSET
        sessions_plus_one = (
            query.order_by(StudioSession.created_at.desc(), StudioSession.id.desc())
            .limit(size + 1)
            .all()
        )
        if len(sessions_plus_one) > size:
            sessions = sessions_plus_one[:size]
            next_cursor = encode_cursor(sessions[-1].created_at, sessions[-1].id)
        else:
            sessions = sessions_plus_one
            next_cursor = None

        items = [to_session_summary(s, is_admin=user.is_admin, current_user_id=user.id) for s in sessions]
        return PaginatedSessionsResponse(
            items=items,
            total=total,
            page=page,
            size=size,
            next_cursor=next_cursor,
        )

    @router.get("/authors", response_model=list[StudioAuthorSummary])
    def list_studio_authors(
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> list[StudioAuthorSummary]:
        """List authors who have recorded studio sessions with GDPR pseudonymization for third-party authors."""
        if user.role == UserRole.admin:
            users = (
                db.query(User.id, User.email, User.name)
                .join(StudioSession, StudioSession.user_id == User.id)
                .distinct()
                .order_by(User.id)
                .all()
            )
            authors: list[StudioAuthorSummary] = []
            for u in users:
                is_own = (u.id == user.id)
                pseudo = get_subject_pseudonym(u.id)
                if is_own:
                    authors.append(
                        StudioAuthorSummary(
                            id=u.id,
                            email=u.email,
                            name=u.name,
                            subject_pseudonym=pseudo,
                            is_pseudonymized=False,
                        )
                    )
                else:
                    authors.append(
                        StudioAuthorSummary(
                            id=u.id,
                            email=f"{pseudo.lower()}@pseudonym.healthkicks.local",
                            name=f"Patient {pseudo}",
                            subject_pseudonym=pseudo,
                            is_pseudonymized=True,
                        )
                    )
            return authors
        return [
            StudioAuthorSummary(
                id=user.id,
                email=user.email,
                name=user.name,
                subject_pseudonym=get_subject_pseudonym(user.id),
                is_pseudonymized=False,
            )
        ]

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

    @router.patch("/{session_id}/trim", response_model=StudioSessionSummary)
    def trim_session(
        session_id: str,
        payload: StudioSessionTrimPayload,
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> StudioSessionSummary:
        """Keep a selected time interval from an unvalidated session."""
        session = get_authorized_session(db, session_id, user)
        if session.is_validated is True:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Impossible d'écourter une session déjà confirmée/validée",
            )
        end_offset_sec = payload.end_offset_sec or payload.new_duration_sec
        if end_offset_sec is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="An end offset is required",
            )
        if end_offset_sec > session.duration_sec:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Le point de fin doit être inférieur ou égal à la durée actuelle",
            )
        if payload.start_offset_sec == 0 and end_offset_sec >= session.duration_sec:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="L'intervalle sélectionné doit être plus court que la session actuelle",
            )

        try:
            retained_count = telemetry_service.trim_session_readings(
                device_id=session.device_id,
                session_id=str(session.id),
                start_offset_sec=payload.start_offset_sec,
                end_offset_sec=end_offset_sec,
            )
        except (BotoCoreError, ClientError) as error:
            logger.error(
                "Could not trim readings from DynamoDB for session %s: %s",
                session.id,
                error,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to trim studio session readings from telemetry store",
            )

        session.duration_sec = end_offset_sec - payload.start_offset_sec
        session.sample_count = retained_count
        db.commit()
        db.refresh(session)
        return to_session_summary(session, is_admin=user.is_admin, current_user_id=user.id)

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

        return to_session_summary(session, is_admin=user.is_admin, current_user_id=user.id)

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
