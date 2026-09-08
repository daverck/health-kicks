"""FastAPI router for DynamoDB IMU telemetry queries and session purge."""

from datetime import datetime
import logging
import uuid

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.deps import CurrentUser
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
    ) -> StudioDatasetStatsResponse:
        """Get dataset statistics (session counts by label) for a specific device."""
        try:
            return telemetry_service.get_dataset_stats(device_id=device_id)
        except (BotoCoreError, ClientError) as error:
            logger.error(
                "DynamoDB error fetching studio stats for device %s: %s",
                device_id,
                error,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to retrieve studio stats from telemetry store",
            )

    @studio_router.get(
        "/stats",
        response_model=StudioDatasetStatsResponse,
        status_code=status.HTTP_200_OK,
    )
    def get_global_studio_stats(
        user: CurrentUser,
    ) -> StudioDatasetStatsResponse:
        """Get dataset statistics (session counts by label) across all devices."""
        try:
            return telemetry_service.get_dataset_stats(device_id=None)
        except (BotoCoreError, ClientError) as error:
            logger.error("DynamoDB error fetching global studio stats: %s", error)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to retrieve studio stats from telemetry store",
            )

    root_router.include_router(devices_router)
    root_router.include_router(studio_router)
    return root_router


