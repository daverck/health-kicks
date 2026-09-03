"""Stateless FastAPI Cloud API entry point."""

import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.auth import create_auth_router
from app.api.v1.cloud import create_cloud_router
from app.api.v1.devices import create_devices_router
from app.api.v1.ingestion import create_ingestion_router
from app.api.v1.users import create_users_router
from app.core.config import settings
from app.db.database import create_tables, engine
from app.services.aws_iot_service import AWSIoTPublishService

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
        try:
            await asyncio.to_thread(run_migrations)
            logger.info("[lifespan] Database migrations completed successfully.")
        except Exception:
            logger.exception("[lifespan] Database migrations failed during startup")
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.public_origins or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(create_cloud_router(publisher))
app.include_router(create_auth_router())
app.include_router(create_devices_router())
app.include_router(create_users_router())
app.include_router(create_ingestion_router())


@app.get("/", tags=["Health"])
def read_root() -> dict[str, str]:
    return {"status": "online", "system": "HealthKicks Cloud API"}
