"""Stateless FastAPI Cloud API entry point."""

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.auth import create_auth_router
from app.api.v1.cloud import create_cloud_router
from app.api.v1.devices import create_devices_router
from app.api.v1.ingestion import create_ingestion_router
from app.api.v1.users import create_users_router
from app.core.config import settings
from app.db.database import create_tables
from app.services.aws_iot_service import AWSIoTPublishService

publisher = AWSIoTPublishService()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Create local schema without connecting to AWS during startup."""
    if settings.auto_create_tables:
        try:
            create_tables()
        except Exception:
            logging.getLogger(__name__).exception("Database unavailable during startup")
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
