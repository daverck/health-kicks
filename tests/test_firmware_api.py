"""Unit and integration tests for firmware distribution endpoint and service."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import get_db
from app.db.models import Base, User, UserRole
from app.main import app
from app.services import token_service
from app.services.firmware_service import FirmwareDistributionService


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def auth_user(db_session) -> User:
    user = User(
        google_sub="sub-firmware-user",
        email="firmware_tester@example.com",
        name="Firmware Tester",
        role=UserRole.user,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def auth_headers(auth_user) -> dict[str, str]:
    token = token_service.issue_access_token(auth_user)
    return {"Authorization": f"Bearer {token}"}


def test_firmware_latest_unauthenticated_rejected(client) -> None:
    """Verify that requests without JWT return 401 Unauthorized."""
    response = client.get("/api/v1/firmware/latest")
    assert response.status_code == 401
    assert "Missing bearer token" in response.json()["detail"]


def test_firmware_latest_success(client, auth_headers, monkeypatch) -> None:
    """Verify 200 OK with metadata and pre-signed URL when S3 object exists."""
    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {
        "Metadata": {
            "version": "v1.2.1-esp32s3",
            "sha256": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        },
        "ContentLength": 805641,
        "LastModified": datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC),
    }
    mock_s3.generate_presigned_url.return_value = (
        "https://healthkicks-firmware-releases.s3.eu-north-1.amazonaws.com/firmware/esp32s3/latest/firmware.bin?AWSAccessKeyId=MOCK"
    )

    monkeypatch.setattr(
        "app.services.firmware_service.boto3.client",
        lambda *args, **kwargs: mock_s3,
    )

    response = client.get("/api/v1/firmware/latest", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == "v1.2.1-esp32s3"
    assert data["sha256"] == "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    assert data["size_bytes"] == 805641
    assert data["download_url"].startswith("https://healthkicks-firmware-releases.s3")
    assert data["expires_in_seconds"] == 900
    assert "release_date" in data


def test_firmware_latest_not_found(client, auth_headers, monkeypatch) -> None:
    """Verify 404 Not Found when S3 head_object raises 404."""
    mock_s3 = MagicMock()
    mock_s3.head_object.side_effect = ClientError(
        error_response={"Error": {"Code": "404", "Message": "Not Found"}},
        operation_name="HeadObject",
    )

    monkeypatch.setattr(
        "app.services.firmware_service.boto3.client",
        lambda *args, **kwargs: mock_s3,
    )

    response = client.get("/api/v1/firmware/latest", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "No firmware release found on S3"


def test_firmware_latest_nosuchkey(client, auth_headers, monkeypatch) -> None:
    """Verify 404 Not Found when S3 head_object raises NoSuchKey."""
    mock_s3 = MagicMock()
    mock_s3.head_object.side_effect = ClientError(
        error_response={"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."}},
        operation_name="HeadObject",
    )

    monkeypatch.setattr(
        "app.services.firmware_service.boto3.client",
        lambda *args, **kwargs: mock_s3,
    )

    response = client.get("/api/v1/firmware/latest", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "No firmware release found on S3"


def test_firmware_latest_s3_client_error_503(client, auth_headers, monkeypatch) -> None:
    """Verify 503 Service Unavailable when S3 client raises generic ClientError."""
    mock_s3 = MagicMock()
    mock_s3.head_object.side_effect = ClientError(
        error_response={"Error": {"Code": "InternalError", "Message": "S3 failure"}},
        operation_name="HeadObject",
    )

    monkeypatch.setattr(
        "app.services.firmware_service.boto3.client",
        lambda *args, **kwargs: mock_s3,
    )

    response = client.get("/api/v1/firmware/latest", headers=auth_headers)
    assert response.status_code == 503
    assert "S3 firmware service error" in response.json()["detail"]


def test_firmware_latest_s3_unexpected_exception_503(client, auth_headers, monkeypatch) -> None:
    """Verify 503 Service Unavailable when S3 client raises an unexpected Exception."""
    mock_s3 = MagicMock()
    mock_s3.head_object.side_effect = RuntimeError("Network timeout connecting to S3")

    monkeypatch.setattr(
        "app.services.firmware_service.boto3.client",
        lambda *args, **kwargs: mock_s3,
    )

    response = client.get("/api/v1/firmware/latest", headers=auth_headers)
    assert response.status_code == 503
    assert "S3 firmware service unavailable" in response.json()["detail"]


def test_firmware_distribution_service_direct_unit() -> None:
    """Direct unit tests on FirmwareDistributionService."""
    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {
        "Metadata": {"version": "v2.0.0", "sha256": "1234"},
        "ContentLength": 5000,
        "LastModified": datetime(2026, 10, 6, 0, 0, 0, tzinfo=UTC),
    }
    mock_s3.generate_presigned_url.return_value = "https://s3.signed/url"

    svc = FirmwareDistributionService(s3_client=mock_s3)
    res = svc.get_latest_firmware()
    assert res.version == "v2.0.0"
    assert res.sha256 == "1234"
    assert res.size_bytes == 5000
    assert res.download_url == "https://s3.signed/url"
    assert res.expires_in_seconds == 900
