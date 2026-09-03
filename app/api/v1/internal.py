"""Internal service routes (e.g. AWS IoT Lifecycle event presence webhooks)."""

from hmac import compare_digest
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import get_db
from app.db.models import Device, DeviceStatus
from app.schemas.internal import DevicePresencePayload, DevicePresenceResponse

logger = logging.getLogger("healthkicks.internal")


def verify_ingest_token(
    x_ingest_token: str | None = Header(default=None, alias="X-Ingest-Token"),
    x_hk_ingest_token: str | None = Header(default=None, alias="X-HealthKicks-Ingest-Token"),
) -> None:
    """Verify that the request comes from an authorized caller using the ingest token."""
    token = x_ingest_token or x_hk_ingest_token
    expected = settings.ingest_token
    if not token or not expected or not compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing ingestion token",
        )


def create_internal_router() -> APIRouter:
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
        device = db.query(Device).filter_by(device_id=payload.device_id).one_or_none()
        if device is None:
            logger.warning("Device presence update rejected: device '%s' not found", payload.device_id)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Device not found",
            )

        norm_status = payload.status.lower().strip()
        if norm_status in ("online", "connected"):
            device.status = DeviceStatus.online
            device.last_seen_utc = payload.timestamp
            logger.info("Device '%s' marked online at %s", device.device_id, payload.timestamp)
        elif norm_status in ("offline", "disconnected"):
            device.status = DeviceStatus.offline
            logger.info("Device '%s' marked offline", device.device_id)
            # Ne pas écraser last_seen_utc lors d'une déconnexion afin de conserver la date du dernier signal reçu
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status '{payload.status}'. Expected 'online', 'offline', 'connected', or 'disconnected'.",
            )

        db.commit()
        db.refresh(device)
        return DevicePresenceResponse(
            status="ok",
            device_id=device.device_id,
            device_status=device.status,
        )

    return router
