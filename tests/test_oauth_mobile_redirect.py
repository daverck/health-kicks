"""Tests for stateless signed OAuth state and mobile deep link redirection."""

import dataclasses
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.auth import (
    generate_oauth_state,
    get_state_platform,
    verify_oauth_state,
)
from app.db.database import get_db
from app.db.models import Base
from app.main import app


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def mock_oauth_settings(monkeypatch):
    """Ensure Google and Azure credentials are configured even in CI environments without .env."""
    import app.api.v1.auth
    import app.core.config
    import app.services.azure_auth_service
    import app.services.google_auth_service

    current = app.core.config.settings
    overrides = {
        "google_client_id": "test-google-client-id",
        "google_client_secret": "test-google-client-secret",
        "google_redirect_uri": "https://healthkicks.duckdns.org/auth/google/callback",
        "google_mobile_redirect_uri": "https://healthkicks.duckdns.org:8443/api/v1/auth/google/callback",
        "azure_client_id": "test-azure-client-id",
        "azure_client_secret": "test-azure-client-secret",
        "azure_tenant_id": "common",
        "azure_redirect_uri": "https://healthkicks.duckdns.org/auth/azure/callback",
        "azure_mobile_redirect_uri": "https://healthkicks.duckdns.org:8443/api/v1/auth/azure/callback",
    }
    new_settings = dataclasses.replace(current, **overrides)
    monkeypatch.setattr(app.core.config, "settings", new_settings)
    monkeypatch.setattr(app.services.google_auth_service, "settings", new_settings)
    monkeypatch.setattr(app.services.azure_auth_service, "settings", new_settings)
    monkeypatch.setattr(app.api.v1.auth, "settings", new_settings)
    return new_settings


@pytest.fixture()
def client(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestOAuthStateLifecycle:
    """Test stateless, cryptographically signed OAuth state generation and verification."""

    def test_state_generation_and_verification_roundtrip(self) -> None:
        state = generate_oauth_state("google", platform="mobile")
        data = verify_oauth_state(state, expected_provider="google")

        assert data["provider"] == "google"
        assert data["platform"] == "mobile"
        assert "nonce" in data and len(data["nonce"]) == 32

    def test_state_rejection_on_tampered_payload(self) -> None:
        state = generate_oauth_state("google", platform="mobile")
        tampered_state = state[:-4] + "xxxx"

        with pytest.raises(HTTPException) as exc_info:
            verify_oauth_state(tampered_state, expected_provider="google")
        assert exc_info.value.status_code == 400
        assert "Invalid OAuth state" in exc_info.value.detail

    def test_state_rejection_on_mismatched_provider(self) -> None:
        state = generate_oauth_state("azure", platform="mobile")

        with pytest.raises(HTTPException) as exc_info:
            verify_oauth_state(state, expected_provider="google")
        assert exc_info.value.status_code == 400
        assert "Invalid OAuth state" in exc_info.value.detail

    def test_state_rejection_on_expiration(self) -> None:
        state = generate_oauth_state("google", platform="mobile")

        # verify with max_age=-1 to simulate immediate expiration
        with pytest.raises(HTTPException) as exc_info:
            verify_oauth_state(state, expected_provider="google", max_age=-1)
        assert exc_info.value.status_code == 400
        assert "expired" in exc_info.value.detail.lower()

    def test_get_state_platform_extraction(self) -> None:
        mobile_state = generate_oauth_state("google", platform="mobile")
        web_state = generate_oauth_state("google", platform="web")
        corrupted_state = "totally_invalid_data"

        assert get_state_platform(mobile_state) == "mobile"
        assert get_state_platform(web_state) == "web"
        assert get_state_platform(corrupted_state) == "web"
        assert get_state_platform(None) == "web"


class TestGoogleOAuthMobileRedirects:
    """Test browser redirect endpoints (/api/v1/auth/google/callback)."""

    def test_google_login_generates_mobile_state_when_redirect_true(self, client) -> None:
        res = client.get("/api/v1/auth/google/login", params={"redirect": "true"}, follow_redirects=False)
        assert res.status_code in (302, 307)
        location = res.headers["location"]
        query_params = parse_qs(urlparse(location).query)
        state = query_params["state"][0]
        assert get_state_platform(state) == "mobile"
        assert query_params["redirect_uri"][0] == "https://healthkicks.duckdns.org:8443/api/v1/auth/google/callback"

    def test_google_login_generates_web_state_when_redirect_false(self, client) -> None:
        res = client.get("/api/v1/auth/google/login", params={"redirect": "false"}, follow_redirects=False)
        assert res.status_code == 200
        data = res.json()
        assert get_state_platform(data["state"]) == "web"
        query_params = parse_qs(urlparse(data["authorization_url"]).query)
        assert query_params["redirect_uri"][0] == "https://healthkicks.duckdns.org/auth/google/callback"

    def test_google_callback_get_success_redirects_to_mobile_deep_link(self, client) -> None:
        mobile_state = generate_oauth_state("google", platform="mobile")
        mock_claims = {
            "sub": "google-user-12345",
            "email": "mobile.user@healthkicks.org",
            "name": "Mobile Tester",
            "picture": "https://example.com/mobile.png",
        }

        with patch("app.services.google_auth_service.exchange_code_for_id_token", return_value=mock_claims) as mock_exchange:
            res = client.get(
                "/api/v1/auth/google/callback",
                params={"code": "valid-oauth-code", "state": mobile_state},
                follow_redirects=False,
            )

            assert res.status_code in (302, 307)
            location = res.headers["location"]
            assert location.startswith("healthkicks://auth/callback")

            parsed = urlparse(location)
            params = parse_qs(parsed.query)
            assert "access_token" in params
            assert "refresh_token" in params
            assert len(params["access_token"][0]) > 0
            mock_exchange.assert_called_once_with("valid-oauth-code", is_mobile=True)

    def test_google_callback_get_success_redirects_to_web_for_web_platform(self, client) -> None:
        web_state = generate_oauth_state("google", platform="web")
        mock_claims = {
            "sub": "google-web-12345",
            "email": "web.user@healthkicks.org",
            "name": "Web Tester",
        }

        with patch("app.services.google_auth_service.exchange_code_for_id_token", return_value=mock_claims) as mock_exchange:
            res = client.get(
                "/api/v1/auth/google/callback",
                params={"code": "valid-oauth-code", "state": web_state},
                follow_redirects=False,
            )

            assert res.status_code in (302, 307)
            location = res.headers["location"]
            assert "healthkicks.duckdns.org" in location
            assert "/auth/google/callback" in location
            params = parse_qs(urlparse(location).query)
            assert "access_token" in params
            mock_exchange.assert_called_once_with("valid-oauth-code", is_mobile=False)

    def test_google_callback_get_provider_error_redirects_to_mobile_deep_link(self, client) -> None:
        mobile_state = generate_oauth_state("google", platform="mobile")

        res = client.get(
            "/api/v1/auth/google/callback",
            params={
                "error": "access_denied",
                "error_description": "User cancelled sign-in",
                "state": mobile_state,
            },
            follow_redirects=False,
        )

        assert res.status_code in (302, 307)
        location = res.headers["location"]
        assert location.startswith("healthkicks://auth/callback?error=")
        assert "User%20cancelled%20sign-in" in location or "User cancelled" in location or "access_denied" in location

    def test_google_callback_get_expired_state_redirects_to_mobile_deep_link(self, client) -> None:
        mobile_state = generate_oauth_state("google", platform="mobile")

        with patch("app.api.v1.auth.verify_oauth_state") as mock_verify:
            mock_verify.side_effect = HTTPException(status_code=400, detail="Expired OAuth state")
            res = client.get(
                "/api/v1/auth/google/callback",
                params={"code": "oauth-code", "state": mobile_state},
                follow_redirects=False,
            )

            assert res.status_code in (302, 307)
            location = res.headers["location"]
            assert location.startswith("healthkicks://auth/callback?error=")
            assert "Expired" in location or "expired" in location

    def test_google_callback_get_exchange_failure_redirects_to_mobile_deep_link(self, client) -> None:
        mobile_state = generate_oauth_state("google", platform="mobile")

        with patch(
            "app.services.google_auth_service.exchange_code_for_id_token",
            side_effect=Exception("Exchange failed: network timeout"),
        ):
            res = client.get(
                "/api/v1/auth/google/callback",
                params={"code": "bad-code", "state": mobile_state},
                follow_redirects=False,
            )

            assert res.status_code in (302, 307)
            location = res.headers["location"]
            assert location.startswith("healthkicks://auth/callback?error=")


class TestAzureOAuthMobileRedirects:
    """Test browser redirect endpoints (/api/v1/auth/azure/callback)."""

    def test_azure_login_generates_mobile_state_when_redirect_true(self, client) -> None:
        res = client.get("/api/v1/auth/azure/login", params={"redirect": "true"}, follow_redirects=False)
        assert res.status_code in (302, 307)
        location = res.headers["location"]
        query_params = parse_qs(urlparse(location).query)
        state = query_params["state"][0]
        assert get_state_platform(state) == "mobile"
        assert query_params["redirect_uri"][0] == "https://healthkicks.duckdns.org:8443/api/v1/auth/azure/callback"

    def test_azure_login_generates_web_state_when_redirect_false(self, client) -> None:
        res = client.get("/api/v1/auth/azure/login", params={"redirect": "false"}, follow_redirects=False)
        assert res.status_code == 200
        data = res.json()
        assert get_state_platform(data["state"]) == "web"
        query_params = parse_qs(urlparse(data["authorization_url"]).query)
        assert query_params["redirect_uri"][0] == "https://healthkicks.duckdns.org/auth/azure/callback"

    def test_azure_callback_get_success_redirects_to_mobile_deep_link(self, client) -> None:
        mobile_state = generate_oauth_state("azure", platform="mobile")
        mock_claims = {
            "azure_sub": "azure-user-99999",
            "email": "azure.mobile@healthkicks.org",
            "name": "Azure Mobile Tester",
        }

        with patch("app.services.azure_auth_service.exchange_code_for_azure_user", return_value=mock_claims) as mock_exchange:
            res = client.get(
                "/api/v1/auth/azure/callback",
                params={"code": "valid-azure-code", "state": mobile_state},
                follow_redirects=False,
            )

            assert res.status_code in (302, 307)
            location = res.headers["location"]
            assert location.startswith("healthkicks://auth/callback")
            params = parse_qs(urlparse(location).query)
            assert "access_token" in params
            assert "refresh_token" in params
            mock_exchange.assert_called_once_with("valid-azure-code", is_mobile=True)


class TestOAuthServiceRedirectUriSelection:
    """Test authorization url and code exchange redirect uri selection."""

    def test_google_auth_url_selects_mobile_and_web_redirect_uri(self) -> None:
        from app.services.google_auth_service import google_authorization_url

        web_url = google_authorization_url("state-web", is_mobile=False)
        mobile_url = google_authorization_url("state-mobile", is_mobile=True)

        web_params = parse_qs(urlparse(web_url).query)
        mobile_params = parse_qs(urlparse(mobile_url).query)

        assert web_params["redirect_uri"][0] == "https://healthkicks.duckdns.org/auth/google/callback"
        assert mobile_params["redirect_uri"][0] == "https://healthkicks.duckdns.org:8443/api/v1/auth/google/callback"

    def test_azure_auth_url_selects_mobile_and_web_redirect_uri(self) -> None:
        from app.services.azure_auth_service import azure_authorization_url

        web_url = azure_authorization_url("state-web", is_mobile=False)
        mobile_url = azure_authorization_url("state-mobile", is_mobile=True)

        web_params = parse_qs(urlparse(web_url).query)
        mobile_params = parse_qs(urlparse(mobile_url).query)

        assert web_params["redirect_uri"][0] == "https://healthkicks.duckdns.org/auth/azure/callback"
        assert mobile_params["redirect_uri"][0] == "https://healthkicks.duckdns.org:8443/api/v1/auth/azure/callback"

    def test_google_token_exchange_posts_expected_redirect_uri(self) -> None:
        from app.services.google_auth_service import exchange_code_for_id_token

        with patch("httpx.post") as mock_post, patch("app.services.google_auth_service.verify_google_id_token", return_value={"sub": "123"}):
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"id_token": "valid.id.token"}

            exchange_code_for_id_token("code-1", is_mobile=True)
            assert mock_post.call_args[1]["data"]["redirect_uri"] == "https://healthkicks.duckdns.org:8443/api/v1/auth/google/callback"

            exchange_code_for_id_token("code-2", is_mobile=False)
            assert mock_post.call_args[1]["data"]["redirect_uri"] == "https://healthkicks.duckdns.org/auth/google/callback"

    def test_azure_token_exchange_posts_expected_redirect_uri(self) -> None:
        from app.services.azure_auth_service import exchange_code_for_azure_user

        with patch("httpx.post") as mock_post, patch("httpx.get") as mock_get:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"access_token": "token-1", "id_token": ""}
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {"mail": "test@healthkicks.org", "id": "az-1"}

            exchange_code_for_azure_user("code-1", is_mobile=True)
            assert mock_post.call_args[1]["data"]["redirect_uri"] == "https://healthkicks.duckdns.org:8443/api/v1/auth/azure/callback"

            exchange_code_for_azure_user("code-2", is_mobile=False)
            assert mock_post.call_args[1]["data"]["redirect_uri"] == "https://healthkicks.duckdns.org/auth/azure/callback"

