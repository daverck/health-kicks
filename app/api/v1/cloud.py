"""Cloud endpoints for haptics, activities and device logs."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.api.v1.utils import validate_and_normalize_date_range
from app.db.database import get_db
from app.db.models import ActivityEvent, Device, DeviceOwnership, HapticLog, UserRole
from app.schemas.cloud import (
    ActivityEventPage,
    ActivityEventResponse,
    HapticLogPage,
    HapticLogResponse,
    HapticTrigger,
    HealthResponse,
)
from app.services.aws_iot_service import AWSIoTPublishService
from app.services.device_service import verify_device_ownership


def _resolve_target_device_ids(device_id: str, user: CurrentUser, db: Session) -> list[str]:
    """Resolve and authorize target device IDs from comma-separated string or 'all'."""
    raw_devs = [d.strip() for d in device_id.split(",") if d.strip()]
    if not raw_devs or any(d.lower() == "all" for d in raw_devs):
        if user.role in (UserRole.admin, UserRole.clinician):
            return [d.device_id for d in db.query(Device.device_id).all()]
        return [o.device_id for o in db.query(DeviceOwnership.device_id).filter_by(user_id=user.id).all()]

    for dev_id in raw_devs:
        verify_device_ownership(db=db, user=user, device_id=dev_id, allow_clinician=True)
    return raw_devs


def create_cloud_router(publisher: AWSIoTPublishService) -> APIRouter:
    """Build HTTP routes with infrastructure dependencies injected."""
    router = APIRouter(prefix="/api/v1", tags=["Cloud API"])

    @router.post("/devices/{device_id}/haptic/trigger")
    def trigger_haptic(device_id: str, command: HapticTrigger, user: CurrentUser, db: Session = Depends(get_db)) -> dict[str, str | int]:
        verify_device_ownership(db=db, user=user, device_id=device_id, allow_clinician=False)
        device = db.query(Device).filter_by(device_id=device_id).one_or_none()
        if device is None:
            db.add(Device(device_id=device_id))
        now = datetime.now(UTC)
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
        event_type: str | None = Query(None, description="Filtre par type d'activité (ex: 'walk', 'run', ou 'falls' pour toutes les chutes)"),
        start_date: datetime | None = Query(None, description="Date/heure de début (inclusive, ISO 8601)"),
        end_date: datetime | None = Query(None, description="Date/heure de fin (inclusive, ISO 8601)"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=100),
        db: Session = Depends(get_db),
        request: Request = None,
    ) -> ActivityEventPage:
        target_device_ids = _resolve_target_device_ids(device_id=device_id, user=user, db=db)
        if not target_device_ids:
            return ActivityEventPage(items=[], page=page, page_size=page_size, total=0)

        raw_end = request.query_params.get("end_date") if request is not None else None
        norm_start, norm_end = validate_and_normalize_date_range(start_date, end_date, raw_end)

        query = db.query(ActivityEvent).filter(ActivityEvent.device_id.in_(target_device_ids))

        if event_type is not None and isinstance(event_type, str):
            clean_types = [t.strip() for t in event_type.split(",") if t.strip()]
            if clean_types and not any(t.lower() == "all" for t in clean_types):
                conditions = []
                exact_types = [t for t in clean_types if t.lower() != "falls"]
                has_falls = any(t.lower() == "falls" for t in clean_types)
                if exact_types:
                    conditions.append(ActivityEvent.event_type.in_(exact_types))
                if has_falls:
                    conditions.append(ActivityEvent.event_type.ilike("%fall%"))
                if conditions:
                    query = query.filter(or_(*conditions))

        if norm_start is not None:
            query = query.filter(ActivityEvent.timestamp_utc >= norm_start)
        if norm_end is not None:
            query = query.filter(ActivityEvent.timestamp_utc <= norm_end)

        total = query.with_entities(func.count(ActivityEvent.id)).scalar() or 0
        events = (
            query.order_by(ActivityEvent.timestamp_utc.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return ActivityEventPage(
            items=[ActivityEventResponse.model_validate(event, from_attributes=True) for event in events],
            page=page,
            page_size=page_size,
            total=total,
        )

    @router.get("/devices/{device_id}/haptic/history", response_model=HapticLogPage)
    @router.get("/devices/{device_id}/haptic/logs", response_model=HapticLogPage)
    def list_haptic_history(
        device_id: str,
        user: CurrentUser,
        start_date: datetime | None = Query(None, description="Date/heure de début (inclusive, ISO 8601)"),
        end_date: datetime | None = Query(None, description="Date/heure de fin (inclusive, ISO 8601)"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=100),
        db: Session = Depends(get_db),
        request: Request = None,
    ) -> HapticLogPage:
        """List haptic commands/vibrations history for a device or multiple devices."""
        target_device_ids = _resolve_target_device_ids(device_id=device_id, user=user, db=db)
        if not target_device_ids:
            return HapticLogPage(items=[], page=page, page_size=page_size, total=0)

        raw_end = request.query_params.get("end_date") if request is not None else None
        norm_start, norm_end = validate_and_normalize_date_range(start_date, end_date, raw_end)

        query = db.query(HapticLog).filter(HapticLog.device_id.in_(target_device_ids))

        if norm_start is not None:
            query = query.filter(HapticLog.triggered_at_utc >= norm_start)
        if norm_end is not None:
            query = query.filter(HapticLog.triggered_at_utc <= norm_end)

        total = query.with_entities(func.count(HapticLog.id)).scalar() or 0
        logs = (
            query.order_by(HapticLog.triggered_at_utc.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return HapticLogPage(
            items=[HapticLogResponse.model_validate(log, from_attributes=True) for log in logs],
            page=page,
            page_size=page_size,
            total=total,
        )

    @router.get("/health", response_model=HealthResponse)
    def health(db: Session = Depends(get_db)) -> HealthResponse:
        try:
            db.execute(text("SELECT 1"))
        except Exception:
            return HealthResponse(status="degraded", database="unavailable")
        return HealthResponse(status="ok", database="ok")

    return router
