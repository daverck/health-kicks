"""Backward-compatible import for ``uvicorn main:app``."""

from app.main import app

__all__ = ["app"]
