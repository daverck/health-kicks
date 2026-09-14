"""Tests for stateless signed OAuth state and mobile deep link redirection."""

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

    def test_google_callback_get_success_redirects_to_mobile_deep_link(self, client) -> None:
        mobile_state = generate_oauth_state("google", platform="mobile")
        mock_claims = {
            "sub": "google-user-12345",
            "email": "mobile.user@healthkicks.org",
            "name": "Mobile Tester",
            "picture": "https://example.com/mobile.png",
        }

        with patch("app.services.google_auth_service.exchange_code_for_id_token", return_value=mock_claims):
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

    def test_google_callback_get_success_redirects_to_web_for_web_platform(self, client) -> None:
        web_state = generate_oauth_state("google", platform="web")
        mock_claims = {
            "sub": "google-web-12345",
            "email": "web.user@healthkicks.org",
            "name": "Web Tester",
        }

        with patch("app.services.google_auth_service.exchange_code_for_id_token", return_value=mock_claims):
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

    def test_azure_callback_get_success_redirects_to_mobile_deep_link(self, client) -> None:
        mobile_state = generate_oauth_state("azure", platform="mobile")
        mock_claims = {
            "azure_sub": "azure-user-99999",
            "email": "azure.mobile@healthkicks.org",
            "name": "Azure Mobile Tester",
        }

        with patch("app.services.azure_auth_service.exchange_code_for_azure_user", return_value=mock_claims):
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
