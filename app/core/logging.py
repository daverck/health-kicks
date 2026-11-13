"""Centralized logging configuration for the HealthKicks Cloud API."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings, settings
from app.core.correlation import CorrelationIdFilter, get_correlation_id

TEXT_LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] [%(correlation_id)s] %(message)s"

_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9\-._~+/]+=*")
_SENSITIVE_KV_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|client_secret|password|secret|token)\s*([=:])\s*([^\s&\"',;]+)"
)

EXTRA_CONTEXT_FIELDS = (
    "user_pseudonym",
    "device_id",
    "event_id",
    "session_id",
    "alert_id",
)


def redact_sensitive_text(text: str) -> str:
    """Scrub bearer tokens and sensitive key-value credentials from log strings."""
    if not text:
        return text
    redacted = _BEARER_RE.sub(r"\1 [REDACTED]", text)
    redacted = _SENSITIVE_KV_RE.sub(r"\1\2[REDACTED]", redacted)
    return redacted


class SensitiveDataRedactionFilter(logging.Filter):
    """Logging filter that redacts credentials and bearer tokens from log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            record.msg = redact_sensitive_text(msg)
            record.args = ()
        except Exception:
            if isinstance(record.msg, str):
                record.msg = redact_sensitive_text(record.msg)
        return True


class StructuredJsonFormatter(logging.Formatter):
    """Single-line JSON log formatter for CloudWatch / centralized log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = (
            datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        correlation_id = getattr(record, "correlation_id", None) or get_correlation_id() or "-"
        message = redact_sensitive_text(record.getMessage())

        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": correlation_id,
            "message": message,
        }

        http_ctx = getattr(record, "http", None)
        if isinstance(http_ctx, dict):
            payload["http"] = http_ctx

        for field_name in EXTRA_CONTEXT_FIELDS:
            val = getattr(record, field_name, None)
            if val is not None:
                payload[field_name] = val

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def resolve_log_format(settings_obj: Settings = settings) -> str:
    """Resolve effective log format ('json' or 'text') from configuration and environment."""
    raw_fmt = str(getattr(settings_obj, "log_format", "auto") or "auto").strip().lower()
    if raw_fmt in ("json", "text"):
        return raw_fmt
    env = str(getattr(settings_obj, "environment", "dev") or "dev").strip().lower()
    return "json" if env in ("prod", "production") else "text"


def setup_logging(settings_obj: Settings = settings) -> None:
    """Configure root, uvicorn, and application loggers with correlation ID and redaction filters."""
    log_level_name = str(getattr(settings_obj, "log_level", "INFO") or "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    effective_format = resolve_log_format(settings_obj)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(CorrelationIdFilter())
    handler.addFilter(SensitiveDataRedactionFilter())

    if effective_format == "json":
        handler.setFormatter(StructuredJsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(TEXT_LOG_FORMAT))

    logging.basicConfig(
        level=log_level,
        handlers=[handler],
        force=True,
    )

    for logger_name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "sqlalchemy.engine",
        "healthkicks",
        "healthkicks.access",
    ):
        logger = logging.getLogger(logger_name)
        logger.setLevel(log_level)
