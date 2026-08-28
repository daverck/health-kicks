"""Secure AWS IoT Rule webhook routes."""

from hmac import compare_digest
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import get_db
from app.schemas.ingestion import IngestionResponse
from app.services.ingestion_service import ingest_event


def _authenticate(token: str | None = Header(default=None, alias="X-HealthKicks-Ingest-Token")) -> None:
    expected = settings.ingest_token
    if expected and not compare_digest(token or "", expected):
        raise HTTPException(status_code=401, detail="Invalid ingestion token")
    if not expected and settings.environment.lower() in {"production", "prod"}:
        raise HTTPException(status_code=503, detail="Ingestion authentication is not configured")


def create_ingestion_router() -> APIRouter:
    """Build the AWS IoT Rule webhook route."""
    router = APIRouter(prefix="/api/v1/ingest", tags=["Ingestion"])

    @router.post("/event", response_model=IngestionResponse, dependencies=[Depends(_authenticate)])
    def ingest_event_webhook(message: dict[str, Any], db: Session = Depends(get_db)) -> IngestionResponse:
        try:
            event = ingest_event(db, message)
        except ValidationError as error:
            raise HTTPException(status_code=422, detail=error.errors()) from error
        msg_id = str(message["header"]["msg_id"])
        return IngestionResponse(status="duplicate" if event is None else "ingested", msg_id=msg_id, duplicate=event is None)

    return router