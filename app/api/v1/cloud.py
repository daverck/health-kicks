import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.core.config import settings
from app.core.metrics import metrics_collector
from app.core.rate_limit import rate_limit
from app.db.database import get_db
from app.db.models import Device, HapticLog
from app.schemas.cloud import (
    ActivityEventPage,
    DatabasePoolStatus,
    HapticLogPage,
    HapticTrigger,
    HealthResponse,
)
from app.services import activity_service
from app.services.aws_iot_service import AWSIoTPublishService
from app.services.device_service import verify_device_ownership


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
        event_type: str | None = Query(None, description="Filter by activity type (e.g. 'walk', 'run', or 'falls' for all falls)"),
        start_date: datetime | None = Query(None, description="Start date/time (inclusive, ISO 8601)"),
        end_date: datetime | None = Query(None, description="End date/time (inclusive, ISO 8601)"),
        cursor: str | None = Query(None, description="Opaque cursor (timestamp_utc|id) for keyset pagination"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=100),
        db: Session = Depends(get_db),
        request: Request = None,  # type: ignore[assignment]
        response: Response = None,  # type: ignore[assignment]
    ) -> ActivityEventPage:
        raw_end = request.query_params.get("end_date") if request is not None else None
        page_obj, next_cursor = activity_service.query_activity_events(
            db=db,
            user=user,
            device_id=device_id,
            event_type=event_type,
            start_date=start_date,
            end_date=end_date,
            raw_end=raw_end,
            cursor=cursor,
            page=page,
            page_size=page_size,
        )
        if response is not None and next_cursor:
            response.headers["X-Next-Cursor"] = next_cursor
        return page_obj

    @router.get("/devices/{device_id}/haptic/history", response_model=HapticLogPage)
    @router.get("/devices/{device_id}/haptic/logs", response_model=HapticLogPage)
    def list_haptic_history(
        device_id: str,
        user: CurrentUser,
        start_date: datetime | None = Query(None, description="Start date/time (inclusive, ISO 8601)"),
        end_date: datetime | None = Query(None, description="End date/time (inclusive, ISO 8601)"),
        cursor: str | None = Query(None, description="Opaque cursor (timestamp_utc|id) for keyset pagination"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=100),
        db: Session = Depends(get_db),
        request: Request = None,  # type: ignore[assignment]
        response: Response = None,  # type: ignore[assignment]
    ) -> HapticLogPage:
        """List haptic commands/vibrations history for a device or multiple devices."""
        raw_end = request.query_params.get("end_date") if request is not None else None
        page_obj, next_cursor = activity_service.query_haptic_logs(
            db=db,
            user=user,
            device_id=device_id,
            start_date=start_date,
            end_date=end_date,
            raw_end=raw_end,
            cursor=cursor,
            page=page,
            page_size=page_size,
        )
        if response is not None and next_cursor:
            response.headers["X-Next-Cursor"] = next_cursor
        return page_obj

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
            bind = db.get_bind()
            engine = getattr(bind, "engine", bind)
            pool = getattr(engine, "pool", getattr(bind, "pool", None))
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
            bind = db.get_bind()
            engine = getattr(bind, "engine", bind)
            pool = getattr(engine, "pool", getattr(bind, "pool", None))
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
