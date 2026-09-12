"""FastAPI router for DynamoDB IMU telemetry queries and session purge."""

from datetime import datetime
from datetime import datetime, timezone
import logging
import uuid

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.db.database import get_db
from app.db.models import StudioSession, User, UserRole
from app.schemas.telemetry import (
    ImuReadingResponse,
    StudioDatasetStatsResponse,
    StudioSessionReadingsResponse,
    StudioStartRequest,
    StudioStartResponse,
)
from app.services.iot_service import IotCommandService
from app.services.telemetry_service import TelemetryService

logger = logging.getLogger(__name__)


def _compute_studio_stats(
    db: Session,
    user: User,
    device_id: str | None = None,
) -> StudioDatasetStatsResponse:
    """Compute studio dataset metrics from PostgreSQL with strict RBAC enforcement."""
    query = db.query(StudioSession)

    if user.role != UserRole.admin:
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


def create_telemetry_router(
    service: TelemetryService | None = None,
    iot_service: IotCommandService | None = None,
) -> APIRouter:
    """Build the telemetry router with injected TelemetryService and IotCommandService."""
    root_router = APIRouter()
    devices_router = APIRouter(prefix="/api/v1/devices", tags=["Telemetry"])
    studio_router = APIRouter(prefix="/api/v1/studio", tags=["Studio"])
    telemetry_service = service or TelemetryService()
    command_service = iot_service or IotCommandService()

    @devices_router.get(
        "/{device_id}/telemetry",
        response_model=StudioSessionReadingsResponse | list[ImuReadingResponse],
    )
    def get_telemetry(
        device_id: str,
        user: CurrentUser,
        session_id: str | None = Query(default=None),
        start_time: datetime | None = Query(default=None),
        end_time: datetime | None = Query(default=None),
        limit: int = Query(default=1000, ge=1, le=2500),
    ) -> StudioSessionReadingsResponse | list[ImuReadingResponse]:
        """Query IMU telemetry by Studio session_id or by time range."""
        if session_id is not None:
            result = telemetry_service.get_session_readings(
                device_id=device_id,
                session_id=session_id,
            )
            if result is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Studio session '{session_id}' not found for device '{device_id}'",
                )
            return result

        if start_time is not None and end_time is not None:
            if start_time > end_time:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="start_time must be less than or equal to end_time",
                )
            start_us = int(start_time.timestamp() * 1_000_000)
            end_us = int(end_time.timestamp() * 1_000_000)
            return telemetry_service.get_timerange_readings(
                device_id=device_id,
                start_epoch_us=start_us,
                end_epoch_us=end_us,
                limit=limit,
            )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide either session_id or both start_time and end_time",
        )

    @devices_router.delete(
        "/{device_id}/telemetry/sessions/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_session_telemetry(
        device_id: str,
        session_id: str,
        user: CurrentUser,
    ) -> Response:
        """Purge all telemetry points associated with a Studio session."""
        telemetry_service.delete_session_readings(
            device_id=device_id,
            session_id=session_id,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @devices_router.post(
        "/{device_id}/commands/studio/start",
        response_model=StudioStartResponse,
        status_code=status.HTTP_200_OK,
    )
    def start_studio_session(
        device_id: str,
        command: StudioStartRequest,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> StudioStartResponse:
        """Trigger a remote Studio IMU recording session on an edge device."""
        session_id = str(uuid.uuid4())
        try:
            topic = command_service.send_studio_start(
                device_id=device_id,
                command=command,
                session_id=session_id,
            )
        except (BotoCoreError, ClientError) as error:
            logger.error(
                "AWS IoT Core error dispatching studio start to %s: %s",
                device_id,
                error,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to dispatch command to device",
            )
        except Exception as error:
            logger.exception(
                "Unexpected error dispatching studio start to %s: %s",
                device_id,
                error,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to dispatch command to device",
            )

        # Immediate PostgreSQL persistence
        session_record = StudioSession(
            id=uuid.UUID(session_id),
            user_id=user.id,
            device_id=device_id,
            label=command.label,
            duration_sec=command.duration_sec,
            sample_count=0,
            created_at=datetime.now(timezone.utc),
        )
        db.add(session_record)
        db.commit()
        db.refresh(session_record)

        return StudioStartResponse(
            status="command_dispatched",
            device_id=device_id,
            session_id=session_id,
            label=command.label,
            duration_sec=command.duration_sec,
            topic=topic,
        )

    @devices_router.get(
        "/{device_id}/studio/stats",
        response_model=StudioDatasetStatsResponse,
        status_code=status.HTTP_200_OK,
    )
    def get_device_studio_stats(
        device_id: str,
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> StudioDatasetStatsResponse:
        """Get dataset statistics (session counts and duration by label) for a specific device."""
        return _compute_studio_stats(db=db, user=user, device_id=device_id)

    @studio_router.get(
        "/stats",
        response_model=StudioDatasetStatsResponse,
        status_code=status.HTTP_200_OK,
    )
    def get_global_studio_stats(
        user: CurrentUser,
        db: Session = Depends(get_db),
    ) -> StudioDatasetStatsResponse:
        """Get dataset statistics (session counts and duration by label) across all devices."""
        return _compute_studio_stats(db=db, user=user, device_id=None)

    root_router.include_router(devices_router)
    root_router.include_router(studio_router)
    return root_router


