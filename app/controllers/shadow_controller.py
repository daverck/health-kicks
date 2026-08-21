"""Device Shadow REST endpoints."""

from fastapi import APIRouter, HTTPException

from app.models.shadow_model import ShadowDocument, ShadowUpdateRequest
from app.services.shadow_service import ShadowService


def create_router(service: ShadowService) -> APIRouter:
    """Create routes bound to the Shadow service."""
    router = APIRouter(prefix="/api/aws/shadow", tags=["AWS IoT Shadow"])

    @router.get("", response_model=ShadowDocument)
    def get_shadow() -> ShadowDocument:
        """Return the last known desired and reported Shadow state."""
        return service.get_state()

    @router.patch("", response_model=ShadowDocument)
    def update_shadow(request: ShadowUpdateRequest) -> ShadowDocument:
        """Publish a desired-state patch and return the local known state."""
        if not service.update_desired(request.state):
            raise HTTPException(status_code=503, detail="AWS IoT Core unavailable")
        return service.get_state()

    return router
