"""Telemetry ingestion service for processing raw IMU batches and updating Aurora DB."""

from typing import Any
from sqlalchemy.orm import Session

from app.db.models import StudioSession
from app.services.ingestion_service import ingest_raw_telemetry


class TelemetryIngestionService:
    """Service to ingest raw telemetry payloads and synchronize Studio session state in Aurora."""

    def __init__(self, db_session: Session | None = None) -> None:
        self._db_session = db_session

    def process_telemetry_batch(
        self,
        db: Session,
        message: dict[str, Any],
        headers: dict[str, Any] | None = None,
    ) -> StudioSession | None:
        """Update studio session sample count and metadata in Aurora DB."""
        return ingest_raw_telemetry(db, message, headers)


__all__ = ["TelemetryIngestionService", "ingest_raw_telemetry"]
