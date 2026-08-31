"""Versioned Cloud API routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, RequireAdmin
from app.db.database import get_db
from app.db.models import Device, FallEvent, HapticLog
from app.schemas.cloud import DeviceResponse, FallEventPage, FallEventResponse, HealthResponse, HapticTrigger
from app.services.aws_iot_service import AWSIoTPublishService


def create_cloud_router(publisher: AWSIoTPublishService) -> APIRouter:
    """Build HTTP routes with infrastructure dependencies injected."""
    router = APIRouter(prefix="/api/v1", tags=["Cloud API"])

    @router.post("/devices/{device_id}/haptic/trigger")
    def trigger_haptic(device_id: str, command: HapticTrigger, user: CurrentUser, db: Session = Depends(get_db)) -> dict[str, str | int]:
        device = db.query(Device).filter_by(device_id=device_id).one_or_none()
        if device is None:
            db.add(Device(device_id=device_id))
        try:
            published = publisher.publish_haptic(device_id, command)
            db.add(HapticLog(device_id=device_id, intensity=command.intensity, duration_ms=command.duration_ms, triggered_by_user=True))
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            raise HTTPException(status_code=500, detail="Unable to persist haptic command")
        if not published:
            raise HTTPException(status_code=503, detail="AWS IoT publish unavailable")
        return {"status": "command_sent", "device_id": device_id, "intensity": command.intensity, "duration_ms": command.duration_ms}

    @router.get("/devices", response_model=list[DeviceResponse])
    def list_devices(user: CurrentUser, db: Session = Depends(get_db)) -> list[Device]:
        return list(db.query(Device).order_by(Device.created_at.desc()).all())

    @router.get("/devices/{device_id}/events/falls", response_model=FallEventPage)
    def list_falls(device_id: str, user: CurrentUser, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100), db: Session = Depends(get_db)) -> FallEventPage:
        query = db.query(FallEvent).filter(FallEvent.device_id == device_id)
        total = query.with_entities(func.count(FallEvent.id)).scalar() or 0
        events = query.order_by(FallEvent.timestamp_utc.desc()).offset((page - 1) * page_size).limit(page_size).all()
        return FallEventPage(items=[FallEventResponse.model_validate(event, from_attributes=True) for event in events], page=page, page_size=page_size, total=total)

    @router.get("/health", response_model=HealthResponse)
    def health(db: Session = Depends(get_db)) -> HealthResponse:
        try:
            db.execute(text("SELECT 1"))
        except Exception:
            return HealthResponse(status="degraded", database="unavailable")
        return HealthResponse(status="ok", database="ok")

    return router