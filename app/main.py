"""Stateless FastAPI Cloud API entry point."""

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.controllers.cloud_controller import create_cloud_router
from app.core.config import settings
from app.core.database import create_tables
from app.services.aws_iot_publish_service import AWSIoTPublishService

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
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(create_cloud_router(publisher))


@app.get("/", tags=["Health"])
def read_root() -> dict[str, str]:
    return {"status": "online", "system": "HealthKicks Cloud API"}
