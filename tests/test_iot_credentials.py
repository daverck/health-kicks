"""Tests for AWS STS IoT credentials token exchange service and endpoint."""

from datetime import datetime, timezone
import json
from unittest.mock import MagicMock

from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db, get_sts_service
from app.core.config import Settings
from app.db.models import Base, Device, DeviceOwnership, DeviceStatus, User, UserRole
from app.main import app
from app.services import token_service
from app.services.aws_sts_service import AWSSTSService


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
def mock_sts_client():
    client = MagicMock()
    client.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "ASIA_MOCK_ACCESS_KEY",
            "SecretAccessKey": "mock_secret_key",
            "SessionToken": "mock_session_token_xyz",
            "Expiration": datetime(2026, 9, 14, 18, 0, 0, tzinfo=timezone.utc),
        }
    }
    return client


@pytest.fixture()
def sts_service(mock_sts_client):
    test_config = Settings(
        aws_region="eu-north-1",
        aws_iot_endpoint="a2k10w7ebf2tx9-ats.iot.eu-north-1.amazonaws.com",
        aws_iot_role_arn="arn:aws:iam::123456789012:role/HealthKicksMobileIoTRole",
        aws_sts_session_duration=3600,
    )
    return AWSSTSService(client=mock_sts_client, config=test_config)


@pytest.fixture()
def client(db_session, sts_service):
    def override_get_db():
        yield db_session

    def override_get_sts():
        return sts_service

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_sts_service] = override_get_sts
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def admin_user(db_session) -> User:
    user = User(
        email="admin@example.com",
        name="Admin User",
        role=UserRole.admin,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def standard_user(db_session) -> User:
    user = User(
        email="user@example.com",
        name="Standard User",
        role=UserRole.user,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def user_device(db_session, standard_user) -> Device:
    device = Device(device_id="HK-1", name="Left Shoe", status=DeviceStatus.online)
    db_session.add(device)
    ownership = DeviceOwnership(user_id=standard_user.id, device_id="HK-1")
    db_session.add(ownership)
    db_session.commit()
    return device


class TestAWSSTSServiceUnit:
    """Unit tests for AWSSTSService session policy builder and credential generator."""

    def test_build_session_policy_single_device(self, sts_service):
        policy_str = sts_service.build_session_policy(user_id=42, device_ids=["HK-1"])
        policy = json.loads(policy_str)

        assert policy["Version"] == "2012-10-17"
        statements = policy["Statement"]
        assert len(statements) == 3

        # Connect statement
        connect_stmt = next(s for s in statements if s["Action"] == ["iot:Connect"])
        assert any("client/*HK-1*" in r for r in connect_stmt["Resource"])
        assert any("client/42-*" in r for r in connect_stmt["Resource"])

        # Publish and Receive statement
        pub_stmt = next(s for s in statements if s["Action"] == ["iot:Publish", "iot:Receive"])
        assert "arn:aws:iot:eu-north-1:123456789012:topic/healthkicks/v1/HK-1/*" in pub_stmt["Resource"]

        # Subscribe statement
        sub_stmt = next(s for s in statements if s["Action"] == ["iot:Subscribe"])
        assert "arn:aws:iot:eu-north-1:123456789012:topicfilter/healthkicks/v1/HK-1/*" in sub_stmt["Resource"]

    def test_build_session_policy_wildcard_admin(self, sts_service):
        policy_str = sts_service.build_session_policy(user_id="admin", device_ids=["*"])
        policy = json.loads(policy_str)

        pub_stmt = next(s for s in policy["Statement"] if s["Action"] == ["iot:Publish", "iot:Receive"])
        assert "arn:aws:iot:eu-north-1:123456789012:topic/healthkicks/v1/*" in pub_stmt["Resource"]

    def test_generate_credentials_calls_assume_role(self, sts_service, mock_sts_client):
        creds = sts_service.generate_iot_credentials(user_id=10, device_ids=["HK-1"])

        assert creds["access_key_id"] == "ASIA_MOCK_ACCESS_KEY"
        assert creds["secret_access_key"] == "mock_secret_key"
        assert creds["session_token"] == "mock_session_token_xyz"
        assert creds["iot_endpoint"] == "a2k10w7ebf2tx9-ats.iot.eu-north-1.amazonaws.com"
        assert creds["region"] == "eu-north-1"

        mock_sts_client.assume_role.assert_called_once()
        call_kwargs = mock_sts_client.assume_role.call_args[1]
        assert call_kwargs["RoleArn"] == "arn:aws:iam::123456789012:role/HealthKicksMobileIoTRole"
        assert call_kwargs["RoleSessionName"] == "healthkicks-session-10"
        assert call_kwargs["DurationSeconds"] == 3600

    def test_generate_credentials_missing_role_arn_raises_503(self):
        service = AWSSTSService(client=MagicMock(), config=Settings(aws_iot_role_arn=""))
        with pytest.raises(Exception) as exc_info:
            service.generate_iot_credentials(user_id=1, device_ids=["HK-1"])
        assert exc_info.value.status_code == 503

    def test_generate_credentials_client_error_raises_502(self, mock_sts_client):
        mock_sts_client.assume_role.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Not authorized to assume role"}},
            "AssumeRole",
        )
        test_config = Settings(aws_iot_role_arn="arn:aws:iam::123456789012:role/Role")
        service = AWSSTSService(client=mock_sts_client, config=test_config)

        with pytest.raises(Exception) as exc_info:
            service.generate_iot_credentials(user_id=1, device_ids=["HK-1"])
        assert exc_info.value.status_code == 502
        assert "AccessDenied" in exc_info.value.detail


class TestIoTCredentialsEndpoint:
    """Integration tests for POST /api/v1/auth/iot-credentials."""

    def test_unauthenticated_request_fails_401(self, client):
        response = client.post("/api/v1/auth/iot-credentials")
        assert response.status_code == 401

    def test_invalid_bearer_token_fails_401(self, client):
        response = client.post(
            "/api/v1/auth/iot-credentials",
            headers={"Authorization": "Bearer invalid-token-xyz"},
        )
        assert response.status_code == 401

    def test_user_without_bound_devices_fails_400(self, client, standard_user):
        token = token_service.issue_access_token(standard_user)
        response = client.post(
            "/api/v1/auth/iot-credentials",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400
        assert "No devices bound" in response.json()["detail"]

    def test_user_with_bound_device_success(self, client, standard_user, user_device, mock_sts_client):
        token = token_service.issue_access_token(standard_user)
        response = client.post(
            "/api/v1/auth/iot-credentials",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["access_key_id"] == "ASIA_MOCK_ACCESS_KEY"
        assert data["secret_access_key"] == "mock_secret_key"
        assert data["session_token"] == "mock_session_token_xyz"
        assert data["iot_endpoint"] == "a2k10w7ebf2tx9-ats.iot.eu-north-1.amazonaws.com"
        assert data["region"] == "eu-north-1"
        assert "expiration" in data

        # Verify the policy passed to STS was scoped to HK-1
        call_kwargs = mock_sts_client.assume_role.call_args[1]
        policy = json.loads(call_kwargs["Policy"])
        pub_stmt = next(s for s in policy["Statement"] if s["Action"] == ["iot:Publish", "iot:Receive"])
        assert any("healthkicks/v1/HK-1/*" in r for r in pub_stmt["Resource"])

    def test_user_requests_owned_device_explicitly(self, client, standard_user, user_device, mock_sts_client):
        token = token_service.issue_access_token(standard_user)
        response = client.post(
            "/api/v1/auth/iot-credentials",
            headers={"Authorization": f"Bearer {token}"},
            json={"device_id": "HK-1"},
        )
        assert response.status_code == 200
        assert response.json()["access_key_id"] == "ASIA_MOCK_ACCESS_KEY"

    def test_user_requests_unowned_device_fails_403(self, client, standard_user, user_device):
        token = token_service.issue_access_token(standard_user)
        response = client.post(
            "/api/v1/auth/iot-credentials",
            headers={"Authorization": f"Bearer {token}"},
            json={"device_id": "HK-OTHER-999"},
        )
        assert response.status_code == 403
        assert "You do not own this device" in response.json()["detail"]

    def test_admin_requests_any_device_succeeds(self, client, admin_user, mock_sts_client):
        token = token_service.issue_access_token(admin_user)
        response = client.post(
            "/api/v1/auth/iot-credentials",
            headers={"Authorization": f"Bearer {token}"},
            json={"device_id": "HK-UNOWNED-123"},
        )
        assert response.status_code == 200
        assert response.json()["access_key_id"] == "ASIA_MOCK_ACCESS_KEY"

        call_kwargs = mock_sts_client.assume_role.call_args[1]
        policy = json.loads(call_kwargs["Policy"])
        pub_stmt = next(s for s in policy["Statement"] if s["Action"] == ["iot:Publish", "iot:Receive"])
        assert any("healthkicks/v1/HK-UNOWNED-123/*" in r for r in pub_stmt["Resource"])

    def test_admin_without_device_gets_wildcard(self, client, admin_user, mock_sts_client):
        token = token_service.issue_access_token(admin_user)
        response = client.post(
            "/api/v1/auth/iot-credentials",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        call_kwargs = mock_sts_client.assume_role.call_args[1]
        policy = json.loads(call_kwargs["Policy"])
        pub_stmt = next(s for s in policy["Statement"] if s["Action"] == ["iot:Publish", "iot:Receive"])
        assert "arn:aws:iot:eu-north-1:123456789012:topic/healthkicks/v1/*" in pub_stmt["Resource"]

    def test_query_parameter_fallback(self, client, standard_user, user_device, mock_sts_client):
        token = token_service.issue_access_token(standard_user)
        response = client.post(
            "/api/v1/auth/iot-credentials?device_id=HK-1",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["access_key_id"] == "ASIA_MOCK_ACCESS_KEY"

