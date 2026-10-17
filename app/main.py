"""Stateless FastAPI Cloud API entry point."""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from alembic.config import Config
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.correlation import (
    CORRELATION_HEADER,
    REQUEST_ID_HEADER,
    correlation_id_ctx,
    sanitize_correlation_id,
)
from app.core.metrics import metrics_collector

from alembic import command
from app.api.v1.auth import create_auth_router
from app.api.v1.cloud import create_cloud_router
from app.api.v1.devices import create_devices_router
from app.api.v1.firmware import create_firmware_router
from app.api.v1.ingestion import create_ingestion_router
from app.api.v1.internal import create_internal_router
from app.api.v1.steps import create_steps_router
from app.api.v1.studio_sessions import create_studio_sessions_router
from app.api.v1.telemetry import create_telemetry_router
from app.api.v1.users import create_users_router
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.database import create_tables, engine
from app.services.aws_iot_service import AWSIoTPublishService

setup_logging()

logger = logging.getLogger("healthkicks.app")
publisher = AWSIoTPublishService()


def run_migrations() -> None:
    """Run Alembic migrations programmatically using the shared SQLAlchemy engine connection."""
    ini_path = Path("alembic.ini")
    if not ini_path.exists():
        ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    alembic_cfg = Config(str(ini_path))
    with engine.begin() as connection:
        alembic_cfg.attributes["connection"] = connection
        command.upgrade(alembic_cfg, "head")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Lifecycle manager handling startup migrations and schema initialization."""
    migrate_env = os.getenv("MIGRATE_ON_START")
    should_migrate = (
        migrate_env.lower() in ("1", "true", "yes")
        if migrate_env is not None
        else getattr(settings, "migrate_on_start", True)
    )

    if should_migrate:
        logger.info("[lifespan] Running database migrations (Alembic upgrade head)...")
        max_retries = 6
        for attempt in range(1, max_retries + 1):
            try:
                await asyncio.to_thread(run_migrations)
                logger.info("[lifespan] Database migrations completed successfully.")
                break
            except Exception:
                if attempt == max_retries:
                    logger.exception("[lifespan] Database migrations failed after %d attempts during startup", max_retries)
                else:
                    logger.warning("[lifespan] Database migration attempt %d failed, retrying in 4s...", attempt)
                    await asyncio.sleep(4)
    elif settings.auto_create_tables:
        try:
            create_tables()
        except Exception:
            logger.exception("[lifespan] Database unavailable during startup")
    yield


app = FastAPI(
    title="HealthKicks Cloud API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next: Any) -> Response:
    raw_cid = request.headers.get(CORRELATION_HEADER.lower()) or request.headers.get(REQUEST_ID_HEADER.lower())
    cid = sanitize_correlation_id(raw_cid)
    token = correlation_id_ctx.set(cid)
    try:
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = cid
        return response
    finally:
        correlation_id_ctx.reset(token)


@app.middleware("http")
async def unhandled_exception_middleware(request: Request, call_next: Any) -> Response:
    try:
        return await call_next(request)
    except Exception as exc:
        logger.exception(f"Unhandled Exception on {request.method} {request.url}: {exc}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal Server Error"},
        )


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "0"
    response.headers["Content-Security-Policy"] = "default-src 'self'"

    is_https = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").lower() == "https"
    )
    is_prod = str(settings.environment).lower() in ("prod", "production")
    if is_https or is_prod:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    return response


@app.middleware("http")
async def metrics_middleware(request: Request, call_next: Any) -> Response:
    start_time = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - start_time) * 1000, 2)
    metrics_collector.record_request(request.method, request.url.path, response.status_code, duration_ms)
    return response


is_prod = str(settings.environment).lower() in ("prod", "production")
cors_origins = list(settings.public_origins) if settings.public_origins else [
    "http://localhost:4200",
    "http://127.0.0.1:4200",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "capacitor://localhost",
]
cors_origin_regex = None if is_prod else r"^https?://(localhost|127\.0\.0\.1)(:[0-9]+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=cors_origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)
app.include_router(create_cloud_router(publisher))
app.include_router(create_auth_router())
app.include_router(create_devices_router())
app.include_router(create_users_router())
app.include_router(create_ingestion_router())
app.include_router(create_internal_router())
app.include_router(create_telemetry_router())
app.include_router(create_studio_sessions_router())
app.include_router(create_steps_router())
app.include_router(create_firmware_router())


@app.get("/", tags=["Health"])
def read_root() -> dict[str, str]:
    return {"status": "online", "system": "HealthKicks Cloud API"}


@app.get("/health", tags=["Health"])
def health_liveness() -> dict[str, Any]:
    """Lightweight operational liveness probe."""
    return {"status": "ok", "uptime_seconds": metrics_collector.get_uptime_seconds()}


@app.get("/metrics", tags=["Health"])
def metrics_summary(request: Request, format: str | None = None) -> Response:
    """Operational metrics summary, supporting standard Prometheus exposition and JSON."""
    accept = request.headers.get("accept", "")
    wants_prometheus = (
        format == "prometheus"
        or "text/plain" in accept
        or "application/openmetrics-text" in accept
    )
    if wants_prometheus and format != "json":
        content = metrics_collector.get_prometheus_text()
        return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")
    return JSONResponse(content=metrics_collector.get_summary())
