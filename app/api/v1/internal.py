"""Internal service routes (e.g. AWS IoT Lifecycle event presence and fall alert webhooks)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import verify_ingest_token
from app.core.config import settings
from app.db.database import get_db
from app.db.models import DeviceStatus
from app.schemas.fall_alert import FallAlertIngestPayload, FallAlertIngestResponse
from app.schemas.internal import DevicePresencePayload, DevicePresenceResponse
from app.services.aws_iot_service import AWSIoTPublishService
from app.services.device_service import (
    handle_user_lwt_disconnect,
    update_single_device_presence,
)
from app.services.email_service import SmtpEmailChannel
from app.services.fall_alert_service import ingest_fall_alert_event

__all__ = ["create_internal_router", "settings"]

logger = logging.getLogger("healthkicks.internal")


def create_internal_router(
    publisher: AWSIoTPublishService | None = None,
    email_channel: SmtpEmailChannel | None = None,
) -> APIRouter:
    """Build internal administrative/infrastructure webhook routes."""
    router = APIRouter(prefix="/api/v1/internal", tags=["Internal"])

    @router.post(
        "/device-presence",
        response_model=DevicePresenceResponse,
        dependencies=[Depends(verify_ingest_token)],
    )
    def update_device_presence(
        payload: DevicePresencePayload,
        db: Session = Depends(get_db),
    ) -> DevicePresenceResponse:
        """Update device connection status and last seen timestamp from IoT lifecycle events."""
        norm_status = payload.effective_state
        if norm_status not in ("online", "connected", "offline", "disconnected"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status '{payload.effective_state}'. Expected 'online', 'offline', 'connected', or 'disconnected'.",
            )

        is_online = norm_status in ("online", "connected")

        if payload.device_id:
            device = update_single_device_presence(
                db=db,
                device_id=payload.device_id,
                is_online=is_online,
                timestamp=payload.timestamp,
            )
            return DevicePresenceResponse(
                status="ok",
                device_id=device.device_id,
                device_status=device.status,
            )

        # LWT user-level bulk disconnect
        handle_user_lwt_disconnect(db=db, user_identifier=payload.user_id)
        return DevicePresenceResponse(
            status="ok",
            device_id=None,
            device_status=DeviceStatus.offline,
        )

    @router.post(
        "/fall-alerts",
        response_model=FallAlertIngestResponse,
        dependencies=[Depends(verify_ingest_token)],
    )
    def ingest_fall_alert(
        payload: FallAlertIngestPayload,
        db: Session = Depends(get_db),
    ) -> FallAlertIngestResponse:
        """Ingest a fall alert uplink event (DETECTED, CONFIRM, CANCEL) relayed from AWS IoT Core."""
        return ingest_fall_alert_event(
            db=db,
            payload=payload,
            email_channel=email_channel,
            iot_publisher=publisher,
        )

    return router
