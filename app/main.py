"""Stateless FastAPI Cloud API entry point."""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from alembic.config import Config
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from alembic import command
from app.api.v1.auth import create_auth_router
from app.api.v1.cloud import create_cloud_router
from app.api.v1.devices import create_devices_router
from app.api.v1.fall_alerts import create_fall_alerts_router
from app.api.v1.firmware import create_firmware_router
from app.api.v1.ingestion import create_ingestion_router
from app.api.v1.internal import create_internal_router
from app.api.v1.steps import create_steps_router
from app.api.v1.studio_sessions import create_studio_sessions_router
from app.api.v1.telemetry import create_telemetry_router
from app.api.v1.users import create_users_router
from app.core.config import settings
from app.core.correlation import (
    CORRELATION_HEADER,
    REQUEST_ID_HEADER,
    TRACEPARENT_HEADER,
    correlation_id_ctx,
    generate_traceparent,
    parse_traceparent,
    sanitize_correlation_id,
)
from app.core.idempotency import (
    IDEMPOTENCY_HEADER,
    IDEMPOTENT_REPLAYED_HEADER,
    IdempotencyOutcome,
    compute_payload_hash,
    compute_request_scope,
    idempotency_store,
    is_idempotency_target,
    is_valid_idempotency_key,
)
from app.core.logging import setup_logging
from app.core.metrics import metrics_collector
from app.db.database import SessionLocal, create_tables, engine
from app.services.aws_iot_service import AWSIoTPublishService
from app.services.fall_alert_service import process_due_fall_alerts

setup_logging()

logger = logging.getLogger("healthkicks.app")
access_logger = logging.getLogger("healthkicks.access")
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


def _run_fall_alert_worker_tick() -> None:
    """Execute one synchronous escalation worker cycle in a dedicated DB session."""
    with SessionLocal() as session:
        process_due_fall_alerts(db=session, iot_publisher=publisher)


async def _fall_alert_escalation_worker() -> None:
    """Background loop checking due PENDING/ESCALATING fall alerts every N seconds."""
    interval = max(1, int(getattr(settings, "fall_alert_worker_interval_sec", 5)))
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_run_fall_alert_worker_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[fall-alert-worker] Tick skipped or failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Lifecycle manager handling startup migrations, schema initialization, and fall alert escalation worker."""
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

    worker_task = asyncio.create_task(_fall_alert_escalation_worker())
    try:
        yield
    finally:
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task


app = FastAPI(
    title="HealthKicks Cloud API",
    version="1.0.0",
    lifespan=lifespan,
)


def _resolve_client_ip(request: Request) -> str:
    """Resolve client IP address from X-Forwarded-For or direct socket connection."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first_hop = forwarded.split(",")[0].strip()
        if first_hop:
            return first_hop
    if request.client and request.client.host:
        return str(request.client.host)
    return "-"


@app.middleware("http")
async def idempotency_middleware(request: Request, call_next: Any) -> Response:
    if not is_idempotency_target(request.method, request.url.path):
        return await call_next(request)

    raw_key = request.headers.get(IDEMPOTENCY_HEADER.lower())
    if raw_key is None or raw_key == "":
        return await call_next(request)

    if not is_valid_idempotency_key(raw_key):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": "Invalid Idempotency-Key header: must be 1 to 128 printable ASCII characters"
            },
        )

    clean_key = raw_key.strip()
    body_bytes = await request.body()
    request_hash = compute_payload_hash(body_bytes)
    scope_key = compute_request_scope(request, clean_key)

    resolution = idempotency_store.acquire_or_get(scope_key, request_hash)
    if resolution.outcome == IdempotencyOutcome.MISMATCH:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": "Idempotency-Key cannot be reused with a different request payload"
            },
        )
    if resolution.outcome == IdempotencyOutcome.IN_PROGRESS:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": "A request with this Idempotency-Key is currently being processed"
            },
        )
    if resolution.outcome == IdempotencyOutcome.REPLAY and resolution.cached_response is not None:
        cached = resolution.cached_response
        return Response(
            content=cached.body.encode("utf-8"),
            status_code=cached.status_code,
            media_type=cached.content_type,
            headers={
                IDEMPOTENCY_HEADER: clean_key,
                IDEMPOTENT_REPLAYED_HEADER: "true",
            },
        )

    try:
        response = await call_next(request)
    except Exception:
        idempotency_store.release(scope_key)
        raise

    body_chunks = [chunk async for chunk in response.body_iterator]
    resp_bytes = b"".join(body_chunks)
    content_type = response.headers.get("content-type", "application/json")

    if 200 <= response.status_code < 300:
        idempotency_store.complete(
            scope_key=scope_key,
            request_hash=request_hash,
            status_code=response.status_code,
            response_body=resp_bytes.decode("utf-8", errors="replace"),
            content_type=content_type,
        )
    else:
        idempotency_store.release(scope_key)

    forwarded_headers = {
        k: v
        for k, v in response.headers.items()
        if k.lower() not in ("content-length", "content-type")
    }
    forwarded_headers[IDEMPOTENCY_HEADER] = clean_key
    forwarded_headers[IDEMPOTENT_REPLAYED_HEADER] = "false"

    return Response(
        content=resp_bytes,
        status_code=response.status_code,
        media_type=content_type,
        headers=forwarded_headers,
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
async def metrics_and_access_log_middleware(request: Request, call_next: Any) -> Response:
    start_time = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - start_time) * 1000, 2)
    path = request.url.path
    status_code = response.status_code

    metrics_collector.record_request(request.method, path, status_code, duration_ms)

    client_ip = _resolve_client_ip(request)
    if path in ("/health", "/metrics") and status_code < 400:
        log_level = logging.DEBUG
    elif status_code >= 500:
        log_level = logging.ERROR
    elif status_code >= 400:
        log_level = logging.WARNING
    else:
        log_level = logging.INFO

    http_meta = {
        "method": request.method,
        "path": path,
        "status_code": status_code,
        "duration_ms": duration_ms,
        "client_ip": client_ip,
    }
    access_logger.log(
        log_level,
        "%s %s %d %.2fms client=%s",
        request.method,
        path,
        status_code,
        duration_ms,
        client_ip,
        extra={"http": http_meta},
    )
    return response


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next: Any) -> Response:
    raw_tp = request.headers.get(TRACEPARENT_HEADER.lower())
    parsed_tp = parse_traceparent(raw_tp)
    if parsed_tp:
        trace_id, _, _ = parsed_tp
        cid = trace_id
        tp = generate_traceparent(trace_id=trace_id)
    else:
        raw_cid = request.headers.get(CORRELATION_HEADER.lower()) or request.headers.get(REQUEST_ID_HEADER.lower())
        cid = sanitize_correlation_id(raw_cid)
        trace_id_hex = cid if (len(cid) == 32 and all(c in "0123456789abcdefABCDEF" for c in cid)) else None
        tp = generate_traceparent(trace_id=trace_id_hex)

    token = correlation_id_ctx.set(cid)
    try:
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = cid
        response.headers[TRACEPARENT_HEADER] = tp
        return response
    finally:
        correlation_id_ctx.reset(token)


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
cors_origin_regex = None if is_prod else r"^https?://(localhost|127\.0\.0\.1)(:[00-9]+)?$"

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
app.include_router(create_internal_router(publisher=publisher))
app.include_router(create_fall_alerts_router(publisher=publisher))
app.include_router(create_telemetry_router(iot_service=publisher))
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
