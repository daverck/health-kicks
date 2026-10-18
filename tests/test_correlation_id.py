"""Unit tests for distributed tracing and X-Correlation-ID propagation."""

import pytest
from fastapi.testclient import TestClient

from app.core.correlation import (
    CORRELATION_HEADER,
    REQUEST_ID_HEADER,
    sanitize_correlation_id,
)
from app.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_sanitize_correlation_id() -> None:
    """Sanitize valid strings, strip dangerous chars, generate UUID for empty/None."""
    assert len(sanitize_correlation_id(None)) == 32
    assert len(sanitize_correlation_id("")) == 32
    assert sanitize_correlation_id("req-12345.abc_XYZ") == "req-12345.abc_XYZ"
    assert sanitize_correlation_id("req 12345!@#$%^&*()") == "req12345"


def test_correlation_id_auto_generated(client: TestClient) -> None:
    """When no correlation header is supplied, API generates and returns X-Correlation-ID."""
    res = client.get("/health")
    assert res.status_code == 200
    assert CORRELATION_HEADER in res.headers
    cid = res.headers[CORRELATION_HEADER]
    assert len(cid) >= 16


def test_correlation_id_propagated_from_header(client: TestClient) -> None:
    """When client passes X-Correlation-ID, the API echoes it back in the response."""
    test_cid = "client-trace-uuid-12345"
    res = client.get("/health", headers={CORRELATION_HEADER: test_cid})
    assert res.status_code == 200
    assert res.headers.get(CORRELATION_HEADER) == test_cid


def test_correlation_id_fallback_to_request_id(client: TestClient) -> None:
    """When client passes X-Request-ID, the API adopts it as X-Correlation-ID."""
    test_rid = "req-id-67890"
    res = client.get("/health", headers={REQUEST_ID_HEADER: test_rid})
    assert res.status_code == 200
    assert res.headers.get(CORRELATION_HEADER) == test_rid

