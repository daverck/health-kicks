"""Re-export of internal router for alternative module layout."""

from app.api.v1.internal import create_internal_router

__all__ = ["create_internal_router"]
