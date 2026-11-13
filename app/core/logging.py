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
_JSON_SENSITIVE_KV_RE = re.compile(
    r"""(?i)(["']?(?:access_token|refresh_token|id_token|google_client_secret|azure_client_secret|client_secret|jwt_secret|smtp_password|ingest_token|pseudonymization_pepper|fall_alert_ack_secret|aws_secret_access_key|x-ingest-token|password|passwd|pwd|secret|token)["']?\s*[:=]\s*)(["'])([^"']+)(\2)"""
)
_SENSITIVE_KV_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|google_client_secret|azure_client_secret|client_secret|jwt_secret|smtp_password|ingest_token|pseudonymization_pepper|fall_alert_ack_secret|aws_secret_access_key|x-ingest-token|password|passwd|pwd|secret|token)\s*([=:])\s*([^\s&\"',;}\]]+)"
)
_EMAIL_PII_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

EXTRA_CONTEXT_FIELDS = (
    "user_pseudonym",
    "device_id",
    "event_id",
    "session_id",
    "alert_id",
)


def redact_sensitive_text(text: str) -> str:
    """Scrub bearer tokens, OAuth/client secrets, passwords, and personal email PII from log strings."""
    if not text:
        return text
    redacted = _BEARER_RE.sub(r"\1 [REDACTED]", text)
    redacted = _JSON_SENSITIVE_KV_RE.sub(r"\1\2[REDACTED]\4", redacted)
    redacted = _SENSITIVE_KV_RE.sub(r"\1\2[REDACTED]", redacted)
    redacted = _EMAIL_PII_RE.sub("[REDACTED_EMAIL]", redacted)
    return redacted


def _redact_value(value: Any) -> Any:
    """Recursively scrub sensitive strings inside dicts, lists, or scalar log attributes."""
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


class SensitiveDataRedactionFilter(logging.Filter):
    """Logging filter that redacts credentials, bearer tokens, and personal email PII from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            record.msg = redact_sensitive_text(msg)
            record.args = ()
        except Exception:
            if isinstance(record.msg, str):
                record.msg = redact_sensitive_text(record.msg)

        http_ctx = getattr(record, "http", None)
        if isinstance(http_ctx, dict):
            record.http = _redact_value(http_ctx)

        for field_name in EXTRA_CONTEXT_FIELDS:
            val = getattr(record, field_name, None)
            if isinstance(val, str):
                setattr(record, field_name, redact_sensitive_text(val))

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
            payload["http"] = _redact_value(http_ctx)

        for field_name in EXTRA_CONTEXT_FIELDS:
            val = getattr(record, field_name, None)
            if val is not None:
                payload[field_name] = _redact_value(val)

        if record.exc_info:
            payload["exception"] = redact_sensitive_text(self.formatException(record.exc_info))

        return json.dumps(payload, ensure_ascii=False, default=str)


class RedactingTextFormatter(logging.Formatter):
    """Standard text formatter that also scrubs sensitive credentials and PII from tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return redact_sensitive_text(formatted)

    def formatException(self, ei: Any) -> str:
        return redact_sensitive_text(super().formatException(ei))


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

    redaction_filter = SensitiveDataRedactionFilter()
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(CorrelationIdFilter())
    handler.addFilter(redaction_filter)

    if effective_format == "json":
        handler.setFormatter(StructuredJsonFormatter())
    else:
        handler.setFormatter(RedactingTextFormatter(TEXT_LOG_FORMAT))

    logging.basicConfig(
        level=log_level,
        handlers=[handler],
        force=True,
    )

    root_logger = logging.getLogger()
    if not any(isinstance(f, SensitiveDataRedactionFilter) for f in root_logger.filters):
        root_logger.addFilter(redaction_filter)

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
        if not any(isinstance(f, SensitiveDataRedactionFilter) for f in logger.filters):
            logger.addFilter(redaction_filter)
