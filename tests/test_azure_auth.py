"""Tests for Microsoft Entra ID (Azure AD) SSO service and API endpoints."""

import dataclasses
from unittest.mock import MagicMock, patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import URLSafeSerializer
import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.auth import create_auth_router
from app.core.config import settings
from app.db.database import get_db
from app.db.models import Base, User, UserRole
from app.services import auth_service, azure_auth_service
from app.services.azure_auth_service import AzureAuthError


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
def mock_azure_settings(monkeypatch):
    def _apply(**overrides):
        import app.api.v1.auth
        import app.core.config
        import app.services.auth_service
        import app.services.azure_auth_service

        current = app.core.config.settings
        new_settings = dataclasses.replace(current, **overrides)
        monkeypatch.setattr(app.core.config, "settings", new_settings)
        monkeypatch.setattr(app.services.azure_auth_service, "settings", new_settings)
        monkeypatch.setattr(app.services.auth_service, "settings", new_settings)
        monkeypatch.setattr(app.api.v1.auth, "settings", new_settings)
        return new_settings

    return _apply


@pytest.fixture()
def auth_client(db_session, mock_azure_settings) -> TestClient:
    mock_azure_settings(
        azure_client_id="test-az-client-id",
        azure_client_secret="test-az-client-secret",
        azure_tenant_id="test-tenant-id",
        azure_redirect_uri="https://app.example.com/callback",
    )

    app = FastAPI()
    app.include_router(create_auth_router())

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


# ---------------------------------------------------------------------------
# Service Unit Tests
# ---------------------------------------------------------------------------


def test_azure_authorization_url_formatting(mock_azure_settings) -> None:
    mock_azure_settings(
        azure_client_id="my-client-id",
        azure_client_secret="my-secret",
        azure_tenant_id="my-tenant",
        azure_redirect_uri="https://myapp.com/callback",
    )

    url = azure_auth_service.azure_authorization_url("state-xyz")
    assert url.startswith("https://login.microsoftonline.com/my-tenant/oauth2/v2.0/authorize?")
    assert "client_id=my-client-id" in url
    assert "response_type=code" in url
    assert "redirect_uri=https%3A%2F%2Fmyapp.com%2Fcallback" in url
    assert "response_mode=query" in url
    assert "scope=openid%20profile%20email%20User.Read" in url
    assert "state=state-xyz" in url


def test_azure_authorization_url_missing_credentials(mock_azure_settings) -> None:
    mock_azure_settings(
        azure_client_id="",
        azure_client_secret="",
    )
    with pytest.raises(AzureAuthError, match="not configured"):
        azure_auth_service.azure_authorization_url("state-xyz")


def test_exchange_code_token_endpoint_failure(mock_azure_settings) -> None:
    mock_azure_settings(
        azure_client_id="cid",
        azure_client_secret="csec",
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "Unauthorized"

    with patch("httpx.post", return_value=mock_resp):
        with pytest.raises(AzureAuthError, match="failed \\(401\\)"):
            azure_auth_service.exchange_code_for_azure_user("bad-code")


def test_email_fallback_order(mock_azure_settings) -> None:
    mock_azure_settings(
        azure_client_id="cid",
        azure_client_secret="csec",
    )

    id_token_payload = {
        "sub": "az-sub-1",
        "email": "token-email@example.com",
        "preferred_username": "token-pref@example.com",
    }
    dummy_id_token = jwt.encode(id_token_payload, "secret", algorithm="HS256")

    token_mock = MagicMock()
    token_mock.status_code = 200
    token_mock.json.return_value = {
        "access_token": "mock-access",
        "id_token": dummy_id_token,
    }

    # Case 1: mail present in Graph profile -> takes top priority
    graph_mock_1 = MagicMock()
    graph_mock_1.status_code = 200
    graph_mock_1.json.return_value = {
        "id": "az-sub-1",
        "displayName": "User One",
        "mail": "primary-mail@example.com",
        "userPrincipalName": "upn@example.com",
    }
    with patch("httpx.post", return_value=token_mock), patch("httpx.get", return_value=graph_mock_1):
        info = azure_auth_service.exchange_code_for_azure_user("code-1")
        assert info["email"] == "primary-mail@example.com"
        assert info["azure_sub"] == "az-sub-1"
        assert info["name"] == "User One"

    # Case 2: mail is None, falls back to userPrincipalName
    graph_mock_2 = MagicMock()
    graph_mock_2.status_code = 200
    graph_mock_2.json.return_value = {
        "id": "az-sub-1",
        "displayName": "User Two",
        "mail": None,
        "userPrincipalName": "upn@example.com",
    }
    with patch("httpx.post", return_value=token_mock), patch("httpx.get", return_value=graph_mock_2):
        info = azure_auth_service.exchange_code_for_azure_user("code-2")
        assert info["email"] == "upn@example.com"

    # Case 3: Graph profile has neither mail nor userPrincipalName -> fallback to id_token email
    graph_mock_3 = MagicMock()
    graph_mock_3.status_code = 200
    graph_mock_3.json.return_value = {
        "id": "az-sub-1",
        "displayName": "User Three",
        "mail": None,
        "userPrincipalName": None,
    }
    with patch("httpx.post", return_value=token_mock), patch("httpx.get", return_value=graph_mock_3):
        info = azure_auth_service.exchange_code_for_azure_user("code-3")
        assert info["email"] == "token-email@example.com"

    # Case 4: Graph profile None, id_token has only preferred_username
    dummy_id_token_pref = jwt.encode(
        {"sub": "az-sub-1", "preferred_username": "token-pref@example.com"},
        "secret",
        algorithm="HS256",
    )
    token_mock_pref = MagicMock()
    token_mock_pref.status_code = 200
    token_mock_pref.json.return_value = {
        "access_token": "mock-access",
        "id_token": dummy_id_token_pref,
    }
    with patch("httpx.post", return_value=token_mock_pref), patch("httpx.get", return_value=graph_mock_3):
        info = azure_auth_service.exchange_code_for_azure_user("code-4")
        assert info["email"] == "token-pref@example.com"

    # Case 5: No email found anywhere -> raises AzureAuthError("No valid email found in Microsoft account")
    dummy_id_token_empty = jwt.encode({"sub": "az-sub-1"}, "secret", algorithm="HS256")
    token_mock_empty = MagicMock()
    token_mock_empty.status_code = 200
    token_mock_empty.json.return_value = {
        "access_token": "mock-access",
        "id_token": dummy_id_token_empty,
    }
    with patch("httpx.post", return_value=token_mock_empty), patch("httpx.get", return_value=graph_mock_3):
        with pytest.raises(AzureAuthError, match="No valid email found in Microsoft account"):
            azure_auth_service.exchange_code_for_azure_user("code-5")


def test_get_or_create_azure_user_new(db_session) -> None:
    user_info = {
        "azure_sub": "az-12345",
        "email": "New.Azure@Example.com",
        "name": "Azure User",
    }
    user = azure_auth_service.get_or_create_azure_user(db_session, user_info)
    assert user.id is not None
    assert user.azure_sub == "az-12345"
    assert user.google_sub is None
    assert user.email == "new.azure@example.com"
    assert user.name == "Azure User"
    assert user.role == UserRole.user
    assert user.auth_provider == "azure"
    assert user.is_active is True
    assert user.last_login_utc is not None


def test_get_or_create_azure_user_links_existing_google_user(db_session) -> None:
    # Existing user registered via Google SSO
    existing = User(
        google_sub="g-orig-sub",
        azure_sub=None,
        email="shared@example.com",
        name="Original Google Name",
        role=UserRole.admin,
        auth_provider="google",
    )
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)

    user_info = {
        "azure_sub": "az-new-sub",
        "email": "Shared@Example.COM",
        "name": "Azure Name",
    }
    linked_user = azure_auth_service.get_or_create_azure_user(db_session, user_info)

    # Must be the exact same user row
    assert linked_user.id == existing.id
    assert linked_user.email == "shared@example.com"
    assert linked_user.azure_sub == "az-new-sub"
    assert linked_user.google_sub == "g-orig-sub"
    assert linked_user.role == UserRole.admin
    # auth_provider must NOT be overwritten
    assert linked_user.auth_provider == "google"


# ---------------------------------------------------------------------------
# Endpoint Integration Tests
# ---------------------------------------------------------------------------


def test_endpoint_azure_login_unconfigured(mock_azure_settings, db_session) -> None:
    mock_azure_settings(azure_client_id="", azure_client_secret="")

    app = FastAPI()
    app.include_router(create_auth_router())
    client = TestClient(app)

    res = client.get("/api/v1/auth/azure/login")
    assert res.status_code == 503


def test_endpoint_azure_login_redirect(auth_client) -> None:
    res = auth_client.get("/api/v1/auth/azure/login", follow_redirects=False)
    assert res.status_code == 307
    location = res.headers["location"]
    assert "https://login.microsoftonline.com/test-tenant-id/oauth2/v2.0/authorize" in location
    assert "client_id=test-az-client-id" in location
    assert "scope=openid+profile+email+User.Read" in location or "scope=openid%20profile%20email%20User.Read" in location


def test_endpoint_azure_login_json(auth_client) -> None:
    res = auth_client.get("/api/v1/auth/azure/login?redirect=false")
    assert res.status_code == 200
    data = res.json()
    assert "authorization_url" in data
    assert "https://login.microsoftonline.com/test-tenant-id/oauth2/v2.0/authorize" in data["authorization_url"]


def test_endpoint_azure_callback_invalid_state(auth_client) -> None:
    res = auth_client.post(
        "/api/v1/auth/azure/callback",
        json={"code": "auth-code", "state": "invalid-state-signature"},
    )
    assert res.status_code == 400
    assert "Invalid OAuth state" in res.json()["detail"]


def test_endpoint_azure_callback_post_success(auth_client, monkeypatch) -> None:
    serializer = URLSafeSerializer(settings.jwt_secret, salt="oauth-state")
    valid_state = serializer.dumps({"nonce": "hk", "provider": "azure"})

    mock_user_info = {
        "azure_sub": "az-sub-777",
        "email": "doctor@healthkicks.org",
        "name": "Dr. House",
        "profile": {},
        "id_token_claims": {},
    }

    with patch(
        "app.services.auth_service.exchange_code_for_azure_user",
        return_value=mock_user_info,
    ):
        res = auth_client.post(
            "/api/v1/auth/azure/callback",
            json={"code": "valid-code", "state": valid_state},
        )
        assert res.status_code == 200
        data = res.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["email"] == "doctor@healthkicks.org"
        assert data["user"]["name"] == "Dr. House"
        assert data["user"]["role"] == "user"
        assert data["user"]["is_active"] is True

        # Verify access token
        claims = auth_service.verify_access_token(data["access_token"])
        assert claims["sub"] == str(data["user"]["id"])
        assert claims["azure_sub"] == "az-sub-777"
        assert claims["email"] == "doctor@healthkicks.org"


def test_endpoint_azure_callback_get_with_frontend_redirect(auth_client, mock_azure_settings) -> None:
    mock_azure_settings(frontend_redirect_url="https://frontend.healthkicks.org/login")

    serializer = URLSafeSerializer(settings.jwt_secret, salt="oauth-state")
    valid_state = serializer.dumps({"nonce": "hk", "provider": "azure"})

    mock_user_info = {
        "azure_sub": "az-sub-888",
        "email": "nurse@healthkicks.org",
        "name": "Nurse Jackie",
        "profile": {},
        "id_token_claims": {},
    }

    with patch(
        "app.services.auth_service.exchange_code_for_azure_user",
        return_value=mock_user_info,
    ):
        res = auth_client.get(
            f"/api/v1/auth/azure/callback?code=valid-code&state={valid_state}",
            follow_redirects=False,
        )
        assert res.status_code == 307
        assert res.headers["location"].startswith("https://frontend.healthkicks.org/login?access_token=")


def test_endpoint_azure_callback_post_token_exchange_error(auth_client) -> None:
    serializer = URLSafeSerializer(settings.jwt_secret, salt="oauth-state")
    valid_state = serializer.dumps({"nonce": "hk", "provider": "azure"})

    with patch(
        "app.services.auth_service.exchange_code_for_azure_user",
        side_effect=AzureAuthError("Azure token exchange failed (401)"),
    ):
        res = auth_client.post(
            "/api/v1/auth/azure/callback",
            json={"code": "expired-code", "state": valid_state},
        )
        assert res.status_code == 401
        assert "Azure token exchange failed" in res.json()["detail"]


def test_endpoint_azure_callback_post_links_existing_google_user(auth_client, db_session) -> None:
    # 1. User pre-exists from Google SSO
    google_user = User(
        google_sub="g-112233",
        azure_sub=None,
        email="linked.doc@healthkicks.org",
        name="Dr. Gregory",
        role=UserRole.clinician,
        auth_provider="google",
    )
    db_session.add(google_user)
    db_session.commit()
    db_session.refresh(google_user)
    user_id = google_user.id

    # 2. Azure SSO callback for the same email
    serializer = URLSafeSerializer(settings.jwt_secret, salt="oauth-state")
    valid_state = serializer.dumps({"nonce": "hk", "provider": "azure"})

    mock_user_info = {
        "azure_sub": "az-445566",
        "email": "linked.doc@healthkicks.org",
        "name": "Dr. Gregory House",
        "profile": {},
        "id_token_claims": {},
    }

    with patch(
        "app.services.auth_service.exchange_code_for_azure_user",
        return_value=mock_user_info,
    ):
        res = auth_client.post(
            "/api/v1/auth/azure/callback",
            json={"code": "valid-code", "state": valid_state},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["user"]["id"] == user_id
        assert data["user"]["email"] == "linked.doc@healthkicks.org"
        assert data["user"]["role"] == "clinician"

        # 3. Verify in DB that account was linked without overriding google_sub or initial auth_provider
        db_user = db_session.query(User).filter_by(id=user_id).one()
        assert db_user.google_sub == "g-112233"
        assert db_user.azure_sub == "az-445566"
        assert db_user.auth_provider == "google"

        # 4. Verify /me endpoint works with the issued token
        me_res = auth_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {data['access_token']}"},
        )
        assert me_res.status_code == 200
        assert me_res.json()["id"] == user_id
        assert me_res.json()["email"] == "linked.doc@healthkicks.org"
        assert me_res.json()["role"] == "clinician"
