"""Distributed tracing correlation ID management for request lifecycle observability."""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar

CORRELATION_HEADER = "X-Correlation-ID"
REQUEST_ID_HEADER = "X-Request-ID"

correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    """Return the active correlation ID for the current async execution context."""
    return correlation_id_ctx.get() or ""


def sanitize_correlation_id(raw_id: str | None) -> str:
    """Validate and sanitize an incoming correlation identifier, or generate a fresh UUID4."""
    if not raw_id:
        return uuid.uuid4().hex
    sanitized = re.sub(r"[^\w\-.]", "", str(raw_id).strip())[:64]
    return sanitized or uuid.uuid4().hex


class CorrelationIdFilter(logging.Filter):
    """Logging filter that injects the current correlation ID into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True
