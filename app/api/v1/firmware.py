"""FastAPI router for firmware distribution and updates."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser
from app.schemas.firmware import FirmwareLatestResponse
from app.services.firmware_service import FirmwareDistributionService


def create_firmware_router(
    service: FirmwareDistributionService | None = None,
) -> APIRouter:
    """Instantiate and configure the firmware router."""
    router = APIRouter(prefix="/api/v1/firmware", tags=["Firmware"])

    def get_service() -> FirmwareDistributionService:
        return service if service is not None else FirmwareDistributionService()

    @router.get(
        "/latest",
        response_model=FirmwareLatestResponse,
        summary="Retrieve latest firmware release metadata and pre-signed download URL",
    )
    def get_latest_firmware(
        user: CurrentUser,
        firmware_service: Annotated[FirmwareDistributionService, Depends(get_service)],
    ) -> FirmwareLatestResponse:
        """Fetch metadata and temporary pre-signed URL to download the latest ESP32-S3 firmware binary."""
        return firmware_service.get_latest_firmware()

    return router
