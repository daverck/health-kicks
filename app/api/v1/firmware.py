"""FastAPI router for S3 firmware binary distribution with JWT access control."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser
from app.schemas.firmware import FirmwareLatestResponse
from app.services.firmware_service import FirmwareDistributionService


def get_firmware_service() -> FirmwareDistributionService:
    """Dependency provider for FirmwareDistributionService."""
    return FirmwareDistributionService()


def create_firmware_router(service: FirmwareDistributionService | None = None) -> APIRouter:
    """Instantiate and configure the firmware distribution router."""
    router = APIRouter(prefix="/api/v1/firmware", tags=["Firmware"])

    @router.get(
        "/latest",
        response_model=FirmwareLatestResponse,
        summary="Get latest ESP32-S3 firmware release pre-signed download URL",
    )
    def get_latest_firmware(
        user: CurrentUser,
        svc: Annotated[FirmwareDistributionService, Depends(get_firmware_service)],
    ) -> FirmwareLatestResponse:
        active_service = service or svc
        return active_service.get_latest_firmware()

    return router

