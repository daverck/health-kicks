"""Internal service routes (e.g. AWS IoT Lifecycle event presence and fall alert webhooks)."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import verify_ingest_token
from app.core.config import settings
from app.db.database import get_db
from app.schemas.fall_alert import FallAlertIngestPayload, FallAlertIngestResponse
from app.schemas.internal import DevicePresencePayload, DevicePresenceResponse
from app.services.aws_iot_service import AWSIoTPublishService
from app.services.device_service import process_device_presence
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
        return process_device_presence(db=db, payload=payload)

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
