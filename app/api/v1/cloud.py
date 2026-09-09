from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, RequireAdmin
from app.db.database import get_db
from app.db.models import ActivityEvent, Device, HapticLog
from app.schemas.cloud import (
    ActivityEventPage,
    ActivityEventResponse,
    HapticLogPage,
    HapticLogResponse,
    HealthResponse,
    HapticTrigger,
)
from app.services.aws_iot_service import AWSIoTPublishService


def create_cloud_router(publisher: AWSIoTPublishService) -> APIRouter:
    """Build HTTP routes with infrastructure dependencies injected."""
    router = APIRouter(prefix="/api/v1", tags=["Cloud API"])

    @router.post("/devices/{device_id}/haptic/trigger")
    def trigger_haptic(device_id: str, command: HapticTrigger, user: CurrentUser, db: Session = Depends(get_db)) -> dict[str, str | int]:
        device = db.query(Device).filter_by(device_id=device_id).one_or_none()
        if device is None:
            db.add(Device(device_id=device_id))
        now = datetime.now(timezone.utc)
        try:
            published = publisher.publish_haptic(device_id, command)
            db.add(HapticLog(
                device_id=device_id,
                intensity=command.intensity,
                duration_ms=command.duration_ms,
                triggered_by_user=True,
                triggered_at_utc=now,
            ))
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise HTTPException(status_code=500, detail="Unable to persist haptic command")
        if not published:
            raise HTTPException(status_code=503, detail="AWS IoT publish unavailable")
        return {"status": "command_sent", "device_id": device_id, "intensity": command.intensity, "duration_ms": command.duration_ms}

    @router.get("/devices/{device_id}/events/activities", response_model=ActivityEventPage)
    def list_activities(
        device_id: str,
        user: CurrentUser,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=100),
        db: Session = Depends(get_db),
    ) -> ActivityEventPage:
        query = db.query(ActivityEvent).filter(ActivityEvent.device_id == device_id)
        total = query.with_entities(func.count(ActivityEvent.id)).scalar() or 0
        events = query.order_by(ActivityEvent.timestamp_utc.desc()).offset((page - 1) * page_size).limit(page_size).all()
        return ActivityEventPage(items=[ActivityEventResponse.model_validate(event, from_attributes=True) for event in events], page=page, page_size=page_size, total=total)

    @router.get("/devices/{device_id}/haptic/history", response_model=HapticLogPage)
    @router.get("/devices/{device_id}/haptic/logs", response_model=HapticLogPage)
    def list_haptic_history(
        device_id: str,
        user: CurrentUser,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=100),
        db: Session = Depends(get_db),
    ) -> HapticLogPage:
        """List haptic commands/vibrations history for a device."""
        query = db.query(HapticLog).filter(HapticLog.device_id == device_id)
        total = query.with_entities(func.count(HapticLog.id)).scalar() or 0
        logs = query.order_by(HapticLog.triggered_at_utc.desc()).offset((page - 1) * page_size).limit(page_size).all()
        return HapticLogPage(items=[HapticLogResponse.model_validate(log, from_attributes=True) for log in logs], page=page, page_size=page_size, total=total)

    @router.get("/health", response_model=HealthResponse)
    def health(db: Session = Depends(get_db)) -> HealthResponse:
        try:
            db.execute(text("SELECT 1"))
        except Exception:
            return HealthResponse(status="degraded", database="unavailable")
        return HealthResponse(status="ok", database="ok")

    return router