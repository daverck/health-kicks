"""Telemetry HTTP endpoints."""

from fastapi import APIRouter

from app.models.telemetry_model import IMUTelemetry
from app.services.telemetry_service import TelemetryService


def create_router(service: TelemetryService) -> APIRouter:
    """Create telemetry routes bound to an application service."""
    router = APIRouter(prefix="/api/telemetry", tags=["Telemetry"])

    @router.get("/latest", response_model=IMUTelemetry | dict[str, str])
    def get_latest_telemetry() -> IMUTelemetry | dict[str, str]:
        """Return the latest IMU sample received through MQTT."""
        latest = service.latest()
        return latest if latest is not None else {"message": "Aucune donnée reçue pour le moment"}

    @router.get("/history")
    def get_telemetry_history() -> dict[str, int | list[IMUTelemetry]]:
        """Return the bounded telemetry history."""
        history = service.history()
        return {"count": len(history), "data": history}

    return router
