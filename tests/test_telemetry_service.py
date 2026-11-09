"""Unit tests for TelemetryService.query_device_telemetry and temporal isolation."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.db.models import User, UserRole
from app.schemas.telemetry import StudioSessionReadingsResponse
from app.services.telemetry_service import TelemetryService


def test_query_device_telemetry_missing_params():
    service = TelemetryService()
    db = MagicMock()
    user = User(id=1, email="test@hk.com", role=UserRole.user)

    with pytest.raises(HTTPException) as exc_info:
        service.query_device_telemetry(
            db=db,
            user=user,
            device_id="HK-1",
            session_id=None,
            start_time=None,
            end_time=None,
        )
    assert exc_info.value.status_code == 400
    assert "Must provide either session_id or both start_time and end_time" in exc_info.value.detail


def test_query_device_telemetry_invalid_time_range():
    service = TelemetryService()
    db = MagicMock()
    user = User(id=1, email="test@hk.com", role=UserRole.user)

    start = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
    end = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)

    with pytest.raises(HTTPException) as exc_info:
        service.query_device_telemetry(
            db=db,
            user=user,
            device_id="HK-1",
            start_time=start,
            end_time=end,
        )
    assert exc_info.value.status_code == 400
    assert "start_time must be less than or equal to end_time" in exc_info.value.detail


def test_query_device_telemetry_session_not_found():
    service = TelemetryService()
    service.get_session_readings = MagicMock(return_value=None)
    db = MagicMock()
    user = User(id=1, email="clinician@hk.com", role=UserRole.clinician)

    with pytest.raises(HTTPException) as exc_info:
        service.query_device_telemetry(
            db=db,
            user=user,
            device_id="HK-1",
            session_id="dummy-session-id",
        )
    assert exc_info.value.status_code == 404
    assert "not found for device 'HK-1'" in exc_info.value.detail


def test_query_device_telemetry_session_success():
    service = TelemetryService()
    mock_response = StudioSessionReadingsResponse(
        device_id="HK-1",
        session_id="valid-session",
        label="walk",
        sample_count=10,
        readings=[],
    )
    service.get_session_readings = MagicMock(return_value=mock_response)
    db = MagicMock()
    user = User(id=1, email="clinician@hk.com", role=UserRole.clinician)

    res = service.query_device_telemetry(
        db=db,
        user=user,
        device_id="HK-1",
        session_id="valid-session",
    )
    assert res == mock_response
