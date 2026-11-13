"""Unit and integration tests for structured JSON logging, sensitive redaction, and HTTP access logs."""

from __future__ import annotations

import json
import logging
import sys

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.correlation import correlation_id_ctx
from app.core.logging import (
    TEXT_LOG_FORMAT,
    RedactingTextFormatter,
    SensitiveDataRedactionFilter,
    StructuredJsonFormatter,
    redact_sensitive_text,
    resolve_log_format,
    setup_logging,
)
from app.main import app


def test_resolve_log_format_auto_and_explicit() -> None:
    """Verify resolve_log_format selects text in dev and json in prod when set to auto."""
    dev_settings = Settings(environment="dev", log_format="auto")
    assert resolve_log_format(dev_settings) == "text"

    prod_settings = Settings(environment="prod", log_format="auto")
    assert resolve_log_format(prod_settings) == "json"

    production_settings = Settings(environment="production", log_format="auto")
    assert resolve_log_format(production_settings) == "json"

    explicit_json = Settings(environment="dev", log_format="json")
    assert resolve_log_format(explicit_json) == "json"

    explicit_text = Settings(environment="prod", log_format="text")
    assert resolve_log_format(explicit_text) == "text"


def test_redact_sensitive_text_scrubs_tokens_secrets_and_personal_emails() -> None:
    """Verify bearer tokens, OAuth/Entra/Google secrets, passwords, and personal emails are scrubbed."""
    raw = (
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig "
        "access_token=secret123 refresh_token:ref456 password=my_pass "
        "client_secret=supersecret google_client_secret=GOCSPX-9999 "
        'azure_client_secret="entra-secret-abc" {"smtp_password": "smtp-val-777"} '
        "token=abc-999 user_email=alice.martin+test@gmail.com contact=bob@healthkicks.org"
    )
    redacted = redact_sensitive_text(raw)
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted
    assert "secret123" not in redacted
    assert "ref456" not in redacted
    assert "my_pass" not in redacted
    assert "supersecret" not in redacted
    assert "GOCSPX-9999" not in redacted
    assert "entra-secret-abc" not in redacted
    assert "smtp-val-777" not in redacted
    assert "abc-999" not in redacted
    assert "alice.martin+test@gmail.com" not in redacted
    assert "bob@healthkicks.org" not in redacted

    assert "Bearer [REDACTED]" in redacted
    assert "access_token=[REDACTED]" in redacted
    assert "refresh_token:[REDACTED]" in redacted
    assert "password=[REDACTED]" in redacted
    assert "client_secret=[REDACTED]" in redacted
    assert "google_client_secret=[REDACTED]" in redacted
    assert 'azure_client_secret="[REDACTED]"' in redacted
    assert '"smtp_password": "[REDACTED]"' in redacted
    assert "token=[REDACTED]" in redacted
    assert redacted.count("[REDACTED_EMAIL]") == 2


def test_sensitive_data_redaction_filter_mutates_record() -> None:
    """Verify SensitiveDataRedactionFilter scrubs formatted log messages and PII emails."""
    record = logging.LogRecord(
        name="healthkicks.auth",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Failed login for patient@example.com with Bearer secret.jwt.token and password=%s",
        args=("hunter2",),
        exc_info=None,
    )
    redaction_filter = SensitiveDataRedactionFilter()
    assert redaction_filter.filter(record) is True
    assert (
        record.getMessage()
        == "Failed login for [REDACTED_EMAIL] with Bearer [REDACTED] and password=[REDACTED]"
    )


def test_redacting_text_formatter_scrubs_exception_tracebacks() -> None:
    """Verify RedactingTextFormatter scrubs secrets and personal emails inside exception tracebacks."""
    formatter = RedactingTextFormatter(TEXT_LOG_FORMAT)
    try:
        raise RuntimeError(
            "OAuth exchange failed for user.name@domain.fr with azure_client_secret=top-secret-entra"
        )
    except RuntimeError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="healthkicks.auth",
        level=logging.ERROR,
        pathname=__file__,
        lineno=25,
        msg="Unhandled auth exception for user.name@domain.fr",
        args=(),
        exc_info=exc_info,
    )
    record.correlation_id = "corr-999"
    formatted = formatter.format(record)
    assert "user.name@domain.fr" not in formatted
    assert "top-secret-entra" not in formatted
    assert "[REDACTED_EMAIL]" in formatted
    assert "azure_client_secret=[REDACTED]" in formatted


def test_structured_json_formatter_outputs_single_line_json() -> None:
    """Verify StructuredJsonFormatter produces valid single-line JSON with context and exception."""
    formatter = StructuredJsonFormatter()
    token = correlation_id_ctx.set("req-corr-12345")
    try:
        try:
            raise ValueError("Simulated failure with token=secret_val")
        except ValueError:
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="healthkicks.ingestion",
            level=logging.ERROR,
            pathname=__file__,
            lineno=42,
            msg="Ingestion error for Bearer my.jwt.tok",
            args=(),
            exc_info=exc_info,
        )
        record.user_pseudonym = "pseudo-abc-123"
        record.device_id = "HK-SHOE-001"
        record.http = {
            "method": "POST",
            "path": "/api/v1/steps",
            "status_code": 500,
            "duration_ms": 12.34,
            "client_ip": "10.0.0.5",
        }

        formatted = formatter.format(record)
        assert "\n" not in formatted
        parsed = json.loads(formatted)

        assert parsed["level"] == "ERROR"
        assert parsed["logger"] == "healthkicks.ingestion"
        assert parsed["correlation_id"] == "req-corr-12345"
        assert parsed["message"] == "Ingestion error for Bearer [REDACTED]"
        assert parsed["user_pseudonym"] == "pseudo-abc-123"
        assert parsed["device_id"] == "HK-SHOE-001"
        assert parsed["http"] == {
            "method": "POST",
            "path": "/api/v1/steps",
            "status_code": 500,
            "duration_ms": 12.34,
            "client_ip": "10.0.0.5",
        }
        assert "ValueError" in parsed["exception"]
        assert parsed["timestamp"].endswith("Z")
    finally:
        correlation_id_ctx.reset(token)


def test_setup_logging_attaches_json_formatter_when_configured() -> None:
    """Verify setup_logging configures root handler with StructuredJsonFormatter in json mode."""
    json_settings = Settings(environment="prod", log_format="json", log_level="INFO")
    setup_logging(json_settings)
    try:
        root_logger = logging.getLogger()
        assert len(root_logger.handlers) >= 1
        assert isinstance(root_logger.handlers[0].formatter, StructuredJsonFormatter)
    finally:
        setup_logging(Settings(environment="dev", log_format="text", log_level="INFO"))


def test_http_access_log_middleware_emits_structured_metadata(caplog) -> None:
    """Verify HTTP requests emit structured access logs with correlation ID and client IP."""
    client = TestClient(app)

    with caplog.at_level(logging.DEBUG, logger="healthkicks.access"):
        response = client.get(
            "/",
            headers={
                "X-Correlation-ID": "custom-corr-id-999",
                "X-Forwarded-For": "203.0.113.42, 10.0.0.1",
            },
        )
        assert response.status_code == 200
        assert response.headers["X-Correlation-ID"] == "custom-corr-id-999"

        health_resp = client.get("/health")
        assert health_resp.status_code == 200

        not_found_resp = client.get("/api/v1/non-existent-route-xyz")
        assert not_found_resp.status_code == 404

    access_records = [r for r in caplog.records if r.name == "healthkicks.access"]
    root_records = [r for r in access_records if getattr(r, "http", {}).get("path") == "/"]
    assert len(root_records) >= 1
    rec = root_records[-1]
    assert rec.levelno == logging.INFO
    assert rec.http["method"] == "GET"
    assert rec.http["path"] == "/"
    assert rec.http["status_code"] == 200
    assert rec.http["client_ip"] == "203.0.113.42"
    assert isinstance(rec.http["duration_ms"], float)

    health_records = [r for r in access_records if getattr(r, "http", {}).get("path") == "/health"]
    assert len(health_records) >= 1
    assert health_records[-1].levelno == logging.DEBUG

    not_found_records = [
        r for r in access_records if getattr(r, "http", {}).get("path") == "/api/v1/non-existent-route-xyz"
    ]
    assert len(not_found_records) >= 1
    assert not_found_records[-1].levelno == logging.WARNING
    assert not_found_records[-1].http["status_code"] == 404
