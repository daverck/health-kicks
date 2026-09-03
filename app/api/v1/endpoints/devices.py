"""Re-export of devices router for backward compatibility and alternative module layout."""

from app.api.v1.devices import create_devices_router

__all__ = ["create_devices_router"]

