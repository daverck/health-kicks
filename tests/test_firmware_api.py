"""Tests for S3 firmware binary distribution endpoint with JWT access control."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.firmware import get_firmware_service
from app.db.database import get_db
from app.db.models import Base, User, UserRole
from app.main import app
from app.services import token_service
from app.services.firmware_service import FirmwareDistributionService


@pytest.fixture()
def db_session():
    """In-memory SQLite session for isolated database tests."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def client(db_session):
    """Test client with database session override."""
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def regular_user(db_session) -> User:
    """Create a regular test user."""
    user = User(
        google_sub="sub-firmware-user",
        email="firmware@example.com",
        name="Firmware Test User",
        role=UserRole.user,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def auth_headers(regular_user) -> dict[str, str]:
    token = token_service.issue_access_token(regular_user)
    return {"Authorization": f"Bearer {token}"}


def test_get_latest_firmware_unauthorized(client) -> None:
    """Requesting latest firmware without JWT Bearer token returns 401."""
    response = client.get("/api/v1/firmware/latest")
    assert response.status_code == 401


def test_get_latest_firmware_success(client, auth_headers) -> None:
    """Requesting latest firmware with valid JWT returns pre-signed S3 URL and metadata."""
    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {
        "Metadata": {
            "version": "v1.2.1-esp32s3",
            "sha256": "8a32f6b3e7d581f148e658091ecb6a93ad5d24d26f047ff690b200b3e55c2aa7",
        },
        "ContentLength": 1048576,
        "LastModified": datetime(2026, 10, 6, 21, 30, tzinfo=timezone.utc),
    }
    mock_s3.generate_presigned_url.return_value = (
        "https://healthkicks-firmware-releases.s3.eu-north-1.amazonaws.com/firmware/esp32s3/latest/firmware.bin?AWSAccessKeyId=test"
    )

    svc = FirmwareDistributionService(s3_client=mock_s3)
    app.dependency_overrides[get_firmware_service] = lambda: svc

    try:
        response = client.get("/api/v1/firmware/latest", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "v1.2.1-esp32s3"
        assert data["sha256"] == "8a32f6b3e7d581f148e658091ecb6a93ad5d24d26f047ff690b200b3e55c2aa7"
        assert data["size_bytes"] == 1048576
        assert data["expires_in_seconds"] == 900
        assert "https://healthkicks-firmware-releases.s3" in data["download_url"]
    finally:
        app.dependency_overrides.pop(get_firmware_service, None)


def test_get_latest_firmware_not_found(client, auth_headers) -> None:
    """When firmware does not exist on S3, returns 404."""
    mock_s3 = MagicMock()
    mock_s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."}},
        "HeadObject",
    )

    svc = FirmwareDistributionService(s3_client=mock_s3)
    app.dependency_overrides[get_firmware_service] = lambda: svc

    try:
        response = client.get("/api/v1/firmware/latest", headers=auth_headers)
        assert response.status_code == 404
        assert response.json()["detail"] == "No firmware release found on S3"
    finally:
        app.dependency_overrides.pop(get_firmware_service, None)


def test_get_latest_firmware_s3_error(client, auth_headers) -> None:
    """When S3 client raises an unexpected ClientError, returns 503."""
    mock_s3 = MagicMock()
    mock_s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
        "HeadObject",
    )

    svc = FirmwareDistributionService(s3_client=mock_s3)
    app.dependency_overrides[get_firmware_service] = lambda: svc

    try:
        response = client.get("/api/v1/firmware/latest", headers=auth_headers)
        assert response.status_code == 503
        assert "S3 firmware service error" in response.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_firmware_service, None)

