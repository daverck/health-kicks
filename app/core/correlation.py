"""Distributed tracing correlation ID management for request lifecycle observability."""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar

CORRELATION_HEADER = "X-Correlation-ID"
REQUEST_ID_HEADER = "X-Request-ID"
TRACEPARENT_HEADER = "traceparent"

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


def parse_traceparent(raw_traceparent: str | None) -> tuple[str, str, str] | None:
    """Parse a standard W3C traceparent header (00-{trace_id}-{span_id}-{flags})."""
    if not raw_traceparent:
        return None
    match = re.match(
        r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$",
        raw_traceparent.strip().lower(),
    )
    if match:
        version, trace_id, span_id, flags = match.groups()
        if trace_id != "0" * 32 and span_id != "0" * 16:
            return trace_id, span_id, flags
    return None


def generate_traceparent(trace_id: str | None = None) -> str:
    """Generate a valid W3C traceparent string conforming to OpenTelemetry specifications."""
    tid = trace_id if (trace_id and len(trace_id) == 32) else uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    return f"00-{tid}-{span_id}-01"


class CorrelationIdFilter(logging.Filter):
    """Logging filter that injects the current correlation ID into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True

