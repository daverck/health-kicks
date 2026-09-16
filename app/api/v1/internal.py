"""Internal service routes (e.g. AWS IoT Lifecycle event presence webhooks)."""

from datetime import datetime, timezone
from hmac import compare_digest
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import get_db
from app.db.models import Device, DeviceOwnership, DeviceStatus, User
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
        norm_status = payload.effective_state
        if norm_status not in ("online", "connected", "offline", "disconnected"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status '{payload.effective_state}'. Expected 'online', 'offline', 'connected', or 'disconnected'.",
            )

        is_online = norm_status in ("online", "connected")

        if payload.device_id:
            device = db.query(Device).filter_by(device_id=payload.device_id).one_or_none()
            if device is None:
                logger.warning("Device presence update rejected: device '%s' not found", payload.device_id)
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Device not found",
                )

            if is_online:
                device.status = DeviceStatus.online
                device.last_seen_utc = payload.timestamp or datetime.now(timezone.utc)
                logger.info("Device '%s' marked online at %s", device.device_id, device.last_seen_utc)
            else:
                device.status = DeviceStatus.offline
                logger.info("Device '%s' marked offline", device.device_id)
                # Ne pas écraser last_seen_utc lors d'une déconnexion afin de conserver la date du dernier signal reçu

            db.commit()
            db.refresh(device)
            logger.info("Relayed device presence update: device=%s, state=%s", device.device_id, device.status)
            return DevicePresenceResponse(
                status="ok",
                device_id=device.device_id,
                device_status=device.status,
            )

        # LWT user-level bulk disconnect
        user_db_id: int | None = None
        if isinstance(payload.user_id, int):
            user_db_id = payload.user_id
        elif isinstance(payload.user_id, str):
            if payload.user_id.isdigit():
                user_db_id = int(payload.user_id)
            else:
                user = db.query(User).filter(
                    (User.google_sub == payload.user_id)
                    | (User.azure_sub == payload.user_id)
                    | (User.email == payload.user_id)
                ).first()
                if user:
                    user_db_id = user.id

        if user_db_id is not None:
            ownerships = db.query(DeviceOwnership).filter_by(user_id=user_db_id).all()
            device_ids = [o.device_id for o in ownerships]
            if device_ids:
                db.query(Device).filter(Device.device_id.in_(device_ids)).update(
                    {Device.status: DeviceStatus.offline},
                    synchronize_session=False,
                )
                logger.info(
                    "User '%s' (id=%s) LWT triggered: marked %d device(s) offline: %s",
                    payload.user_id,
                    user_db_id,
                    len(device_ids),
                    device_ids,
                )
            else:
                logger.info("User '%s' (id=%s) has no bound devices to update", payload.user_id, user_db_id)
        else:
            logger.warning("User '%s' not found for LWT presence update", payload.user_id)

        db.commit()
        logger.info("Relayed user LWT presence update: user_id=%s, state=offline", payload.user_id)
        return DevicePresenceResponse(
            status="ok",
            device_id=None,
            device_status=DeviceStatus.offline,
        )

    return router

