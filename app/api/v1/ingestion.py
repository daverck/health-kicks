"""Secure AWS IoT Rule webhook routes."""

from hmac import compare_digest
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import verify_ingest_token
from app.core.config import settings
from app.db.database import get_db
from app.schemas.ingestion import IngestionResponse
from app.services.ingestion_service import ingest_event, ingest_raw_telemetry


def create_ingestion_router() -> APIRouter:
    """Build the AWS IoT Rule webhook route."""
    router = APIRouter(prefix="/api/v1/ingest", tags=["Ingestion"])

    @router.post("/event", response_model=IngestionResponse, dependencies=[Depends(verify_ingest_token)])
    def ingest_event_webhook(message: dict[str, Any], db: Session = Depends(get_db)) -> IngestionResponse:
        try:
            event = ingest_event(db, message)
        except ValidationError as error:
            raise HTTPException(status_code=422, detail=error.errors()) from error
        msg_id = str(message["header"]["msg_id"])
        return IngestionResponse(status="duplicate" if event is None else "ingested", msg_id=msg_id, duplicate=event is None)

    @router.post("/telemetry/raw", dependencies=[Depends(verify_ingest_token)])
    @router.post("/event/telemetry/raw", dependencies=[Depends(verify_ingest_token)])
    def ingest_raw_telemetry_webhook(message: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
        session = ingest_raw_telemetry(db, message)
        return {
            "status": "ingested" if session is not None else "ignored",
            "session_id": str(session.id) if session else None,
            "sample_count": session.sample_count if session else 0,
        }

    return router