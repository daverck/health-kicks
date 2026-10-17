import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import and_, func, or_, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.api.v1.utils import validate_and_normalize_date_range
from app.core.config import settings
from app.core.metrics import metrics_collector
from app.core.rate_limit import rate_limit
from app.db.database import get_db
from app.db.models import ActivityEvent, Device, DeviceOwnership, HapticLog, UserRole
from app.schemas.cloud import (
    ActivityEventPage,
    ActivityEventResponse,
    DatabasePoolStatus,
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

    @router.post(
        "/devices/{device_id}/haptic/trigger",
        dependencies=[Depends(rate_limit("haptic_trigger", max_requests=10, window_seconds=60.0))],
    )
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

        # Temporal isolation for regular users: cannot view historical data from before device binding
        if user.role == UserRole.user:
            ownerships = (
                db.query(DeviceOwnership.device_id, DeviceOwnership.bound_at_utc)
                .filter(DeviceOwnership.user_id == user.id, DeviceOwnership.device_id.in_(target_device_ids))
                .all()
            )
            if not ownerships:
                return ActivityEventPage(items=[], page=page, page_size=page_size, total=0)
            bound_conds = []
            for dev_id, bound_at in ownerships:
                if bound_at is not None:
                    bound_conds.append(and_(ActivityEvent.device_id == dev_id, ActivityEvent.timestamp_utc >= bound_at))
                else:
                    bound_conds.append(ActivityEvent.device_id == dev_id)
            if bound_conds:
                query = query.filter(or_(*bound_conds))

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

        user_owned_devs: set[str] = set()
        if user.role in (UserRole.admin, UserRole.clinician):
            user_owned_devs = {
                o.device_id
                for o in db.query(DeviceOwnership.device_id).filter_by(user_id=user.id).all()
            }

        items = [
            ActivityEventResponse(
                id=event.id,
                device_id=event.device_id,
                event_type=event.event_type,
                timestamp_utc=event.timestamp_utc,
                confidence_score=event.confidence_score,
                is_pseudonymized=(user.role in (UserRole.admin, UserRole.clinician) and event.device_id not in user_owned_devs),
            )
            for event in events
        ]

        return ActivityEventPage(
            items=items,
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

        # Temporal isolation for regular users: cannot view historical haptics from before device binding
        if user.role == UserRole.user:
            ownerships = (
                db.query(DeviceOwnership.device_id, DeviceOwnership.bound_at_utc)
                .filter(DeviceOwnership.user_id == user.id, DeviceOwnership.device_id.in_(target_device_ids))
                .all()
            )
            if not ownerships:
                return HapticLogPage(items=[], page=page, page_size=page_size, total=0)
            bound_conds = []
            for dev_id, bound_at in ownerships:
                if bound_at is not None:
                    bound_conds.append(and_(HapticLog.device_id == dev_id, HapticLog.triggered_at_utc >= bound_at))
                else:
                    bound_conds.append(HapticLog.device_id == dev_id)
            if bound_conds:
                query = query.filter(or_(*bound_conds))

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
        """Deep health check verifying relational database connection and latency."""
        start_ping = time.monotonic()
        try:
            db.execute(text("SELECT 1"))
            latency_ms = round((time.monotonic() - start_ping) * 1000, 2)
            db_status = "ok"
        except Exception:
            latency_ms = round((time.monotonic() - start_ping) * 1000, 2)
            return HealthResponse(
                status="degraded",
                database="unavailable",
                uptime_seconds=metrics_collector.get_uptime_seconds(),
                database_latency_ms=latency_ms,
                version="0.1.0",
                environment=settings.environment,
            )

        pool_status: DatabasePoolStatus | None = None
        try:
            pool = db.get_bind().pool
            if pool is not None:
                pool_status = DatabasePoolStatus(
                    size=int(getattr(pool, "size", lambda: 0)()),
                    checked_in=int(getattr(pool, "checkedin", lambda: 0)()),
                    checked_out=int(getattr(pool, "checkedout", lambda: 0)()),
                    overflow=int(getattr(pool, "overflow", lambda: 0)()),
                )
        except Exception:
            pool_status = None

        return HealthResponse(
            status="ok",
            database=db_status,
            uptime_seconds=metrics_collector.get_uptime_seconds(),
            database_latency_ms=latency_ms,
            version="0.1.0",
            environment=settings.environment,
            database_pool=pool_status,
        )

    @router.get("/health/liveness")
    def health_liveness() -> dict[str, str]:
        """Fast container/Kubernetes liveness probe without database dependency."""
        return {"status": "ok"}

    @router.get("/health/readiness", response_model=HealthResponse)
    def health_readiness(db: Session = Depends(get_db)) -> HealthResponse:
        """Container/Kubernetes readiness probe verifying database connectivity."""
        return health(db=db)

    @router.get("/metrics")
    def get_metrics(
        request: Request,
        format: str | None = None,
        db: Session = Depends(get_db),
    ) -> Response:
        """Operational metrics and request latency breakdown."""
        database_pool: dict[str, Any] | None = None
        try:
            pool = db.get_bind().pool
            if pool is not None:
                database_pool = {
                    "size": int(getattr(pool, "size", lambda: 0)()),
                    "checked_in": int(getattr(pool, "checkedin", lambda: 0)()),
                    "checked_out": int(getattr(pool, "checkedout", lambda: 0)()),
                    "overflow": int(getattr(pool, "overflow", lambda: 0)()),
                }
        except Exception:
            pass

        accept = request.headers.get("accept", "")
        wants_prometheus = (
            format == "prometheus"
            or "text/plain" in accept
            or "application/openmetrics-text" in accept
        )
        if wants_prometheus and format != "json":
            content = metrics_collector.get_prometheus_text(database_pool=database_pool)
            return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")

        summary = metrics_collector.get_summary()
        if database_pool is not None:
            summary["database_pool"] = database_pool
        return JSONResponse(content=summary)

    return router
