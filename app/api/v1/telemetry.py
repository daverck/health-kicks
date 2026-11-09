"""FastAPI router for DynamoDB IMU telemetry queries and session purge."""

import logging
import uuid
from datetime import datetime

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, RequireClinicianOrAdmin
from app.db.database import get_db
from app.schemas.telemetry import (
    ImuReadingResponse,
    StudioClassDetailStatsResponse,
    StudioClassesSummaryResponse,
    StudioDatasetStatsResponse,
    StudioSessionReadingsResponse,
    StudioStartRequest,
    StudioStartResponse,
)
from app.services.device_service import verify_device_ownership
from app.services.iot_service import IotCommandService
from app.services.studio_service import (
    compute_class_detail_stats,
    compute_classes_summary,
    compute_studio_stats,
)
from app.services.telemetry_service import TelemetryService

logger = logging.getLogger(__name__)


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
        db: Session = Depends(get_db),
        session_id: str | None = Query(default=None),
        start_time: datetime | None = Query(default=None),
        end_time: datetime | None = Query(default=None),
        limit: int = Query(default=1000, ge=1, le=2500),
    ) -> StudioSessionReadingsResponse | list[ImuReadingResponse]:
        """Query IMU telemetry by Studio session_id or by time range."""
        verify_device_ownership(db=db, user=user, device_id=device_id, allow_clinician=True)
        return telemetry_service.query_device_telemetry(
            db=db,
            user=user,
            device_id=device_id,
            session_id=session_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

    @devices_router.delete(
        "/{device_id}/telemetry/sessions/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_session_telemetry(
        device_id: str,
        session_id: str,
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> Response:
        """Purge all telemetry points associated with a Studio session."""
        verify_device_ownership(db=db, user=user, device_id=device_id, allow_clinician=True)
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
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> StudioStartResponse:
        """Trigger a remote Studio IMU recording session on an edge device."""
        verify_device_ownership(db=db, user=user, device_id=device_id, allow_clinician=True)

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
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> StudioDatasetStatsResponse:
        """Get dataset statistics (session counts and duration by label) for a specific device."""
        verify_device_ownership(db=db, user=user, device_id=device_id, allow_clinician=True)
        return compute_studio_stats(db=db, user=user, device_id=device_id)

    @studio_router.get(
        "/stats",
        response_model=StudioDatasetStatsResponse,
        status_code=status.HTTP_200_OK,
    )
    def get_global_studio_stats(
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> StudioDatasetStatsResponse:
        """Get dataset statistics (session counts and duration by label) across all devices."""
        return compute_studio_stats(db=db, user=user, device_id=None)

    @studio_router.get(
        "/classes/summary",
        response_model=StudioClassesSummaryResponse,
        status_code=status.HTTP_200_OK,
    )
    def get_studio_classes_summary(
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> StudioClassesSummaryResponse:
        """Get global summary and comparative metrics across all activity classes."""
        return compute_classes_summary(db=db, user=user)

    @studio_router.get(
        "/classes/{label}/stats",
        response_model=StudioClassDetailStatsResponse,
        status_code=status.HTTP_200_OK,
    )
    def get_studio_class_stats(
        label: str,
        user: RequireClinicianOrAdmin,
        db: Session = Depends(get_db),
    ) -> StudioClassDetailStatsResponse:
        """Get detailed daily counts, duration distribution, and ML metrics for a specific activity class."""
        return compute_class_detail_stats(db=db, user=user, label=label)


    root_router.include_router(devices_router)
    root_router.include_router(studio_router)
    return root_router
