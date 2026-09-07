"""FastAPI router for DynamoDB IMU telemetry queries and session purge."""

from datetime import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.deps import CurrentUser
from app.schemas.telemetry import ImuReadingResponse, StudioSessionReadingsResponse
from app.services.telemetry_service import TelemetryService

logger = logging.getLogger(__name__)


def create_telemetry_router(service: TelemetryService | None = None) -> APIRouter:
    """Build the telemetry router with injected TelemetryService."""
    router = APIRouter(prefix="/api/v1/devices", tags=["Telemetry"])
    telemetry_service = service or TelemetryService()

    @router.get(
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

    @router.delete(
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

    return router
