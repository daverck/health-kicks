"""Tests for Google SSO auth service, security dependencies and protected routes."""

import dataclasses

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_current_user, require_roles
from app.db.models import Base, User, UserRole
from app.services import google_auth_service, token_service


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
def mock_google_settings(monkeypatch):
    def _apply(**overrides):
        import app.api.v1.auth
        import app.core.config
        import app.services.google_auth_service

        current = app.core.config.settings
        new_settings = dataclasses.replace(current, **overrides)
        monkeypatch.setattr(app.core.config, "settings", new_settings)
        monkeypatch.setattr(app.services.google_auth_service, "settings", new_settings)
        monkeypatch.setattr(app.api.v1.auth, "settings", new_settings)
        return new_settings

    return _apply


def test_issue_and_verify_access_token_roundtrip(db_session) -> None:
    user = User(google_sub="sub-1", email="a@example.com", role=UserRole.clinician)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    token = token_service.issue_access_token(user)
    claims = token_service.verify_access_token(token)
    assert claims["sub"] == str(user.id)
    assert claims["role"] == "clinician"
    assert claims["email"] == "a@example.com"
    assert claims["exp"] > claims["iat"]


def test_verify_rejects_tampered_token() -> None:

    token = token_service.issue_access_token(
        User(google_sub="s", email="b@example.com", role=UserRole.user)
    )
    with pytest.raises(Exception):
        token_service.verify_access_token(token + "x")


def test_get_or_create_user_jit_provisions_once(db_session) -> None:
    claims = {"sub": "g-1", "email": "New@Example.com", "name": "New User", "picture": "p"}
    first = google_auth_service.get_or_create_user(db_session, claims)
    second = google_auth_service.get_or_create_user(db_session, claims)
    assert first.id == second.id
    assert first.email == "new@example.com"
    assert first.role == UserRole.user
    assert first.last_login_utc is not None


def test_get_or_create_user_rebinds_existing_email(db_session) -> None:
    existing = User(google_sub="old-sub", email="x@example.com", role=UserRole.admin)
    db_session.add(existing)
    db_session.commit()
    user = google_auth_service.get_or_create_user(db_session, {"sub": "new-sub", "email": "x@example.com"})
    assert user.id == existing.id
    assert user.google_sub == "new-sub"  # sub refreshed, role preserved


def _protected_app(db_session) -> FastAPI:
    from fastapi import Depends

    app = FastAPI()

    def override_db():
        yield db_session

    app.dependency_overrides[get_current_user.__globals__["get_db"]] = override_db

    @app.get("/admin-only")
    def admin_only(user: User = Depends(require_roles(UserRole.admin))):
        return {"role": user.role.value}

    @app.get("/me")
    def me(user: User = Depends(get_current_user)):
        return {"email": user.email}

    return app


def test_role_authorization_rejects_non_admin(db_session) -> None:
    user = User(google_sub="s", email="c@example.com", role=UserRole.clinician)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    from app.db.database import get_db
    from app.api.deps import bearer_scheme

    app = _protected_app(db_session)
    token = token_service.issue_access_token(user)

    def fake_bearer():
        import fastapi.security as sec
        return sec.HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    app.dependency_overrides[bearer_scheme] = fake_bearer
    client = TestClient(app)
    assert client.get("/admin-only").status_code == 403
    assert client.get("/me").json()["email"] == "c@example.com"

    admin = User(google_sub="s2", email="d@example.com", role=UserRole.admin)
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    admin_token = token_service.issue_access_token(admin)

    def fake_bearer_admin():
        import fastapi.security as sec
        return sec.HTTPAuthorizationCredentials(scheme="Bearer", credentials=admin_token)

    app.dependency_overrides[bearer_scheme] = fake_bearer_admin
    assert client.get("/admin-only").status_code == 200


def test_missing_token_is_unauthorized(db_session) -> None:
    from app.api.deps import bearer_scheme

    app = _protected_app(db_session)
    client = TestClient(app)
    response = client.get("/me")
    assert response.status_code == 401


def test_endpoint_google_login_unconfigured(mock_google_settings) -> None:
    mock_google_settings(google_client_id="", google_client_secret="")
    from app.main import app

    client = TestClient(app)
    res = client.get("/api/v1/auth/google/login")
    assert res.status_code == 503
    assert "Google SSO is not configured" in res.json()["detail"]


def test_endpoint_google_login_json(mock_google_settings) -> None:
    mock_google_settings(
        google_client_id="g-client-id",
        google_client_secret="g-client-secret",
        google_redirect_uri="http://localhost:4200/auth/google/callback",
    )
    from app.main import app

    client = TestClient(app)
    res = client.get("/api/v1/auth/google/login", params={"redirect": "false"})
    assert res.status_code == 200
    data = res.json()
    assert "authorization_url" in data
    assert "state" in data
    assert "accounts.google.com" in data["authorization_url"]
    assert "client_id=g-client-id" in data["authorization_url"]


def test_endpoint_google_login_redirect(mock_google_settings) -> None:
    mock_google_settings(
        google_client_id="g-client-id",
        google_client_secret="g-client-secret",
        google_redirect_uri="http://localhost:4200/auth/google/callback",
    )
    from app.main import app

    client = TestClient(app)
    res = client.get("/api/v1/auth/google/login", params={"redirect": "true"}, follow_redirects=False)
    assert res.status_code in (302, 307)
    assert "accounts.google.com" in res.headers["location"]


def test_endpoint_google_callback_invalid_state() -> None:
    from app.main import app

    client = TestClient(app)
    res = client.post(
        "/api/v1/auth/google/callback",
        json={"code": "g-code", "state": "invalid-signature"},
    )
    assert res.status_code == 400
    assert "Invalid OAuth state" in res.json()["detail"]


def test_endpoint_google_callback_post_success(db_session, monkeypatch) -> None:
    import secrets
    from unittest.mock import patch
    from itsdangerous import URLSafeSerializer
    from app.core.config import settings
    from app.db.database import get_db
    from app.main import app

    serializer = URLSafeSerializer(settings.jwt_secret, salt="oauth-state")
    valid_state = serializer.dumps({"nonce": secrets.token_urlsafe(16), "provider": "google"})

    mock_claims = {
        "sub": "google-sub-999",
        "email": "test.google@healthkicks.org",
        "name": "Google Tester",
        "picture": "https://example.com/avatar.png",
    }

    app.dependency_overrides[get_db] = lambda: db_session

    with patch(
        "app.services.google_auth_service.exchange_code_for_id_token",
        return_value=mock_claims,
    ):
        client = TestClient(app)
        res = client.post(
            "/api/v1/auth/google/callback",
            json={"code": "valid-code", "state": valid_state},
        )
        assert res.status_code == 200
        data = res.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["email"] == "test.google@healthkicks.org"
        assert data["user"]["name"] == "Google Tester"

        claims = token_service.verify_access_token(data["access_token"])
        assert claims["google_sub"] == "google-sub-999"
        assert claims["email"] == "test.google@healthkicks.org"


def test_endpoint_google_callback_post_token_exchange_error() -> None:
    import secrets
    from unittest.mock import patch
    from itsdangerous import URLSafeSerializer
    from app.core.config import settings
    from app.services.google_auth_service import GoogleAuthError
    from app.main import app

    serializer = URLSafeSerializer(settings.jwt_secret, salt="oauth-state")
    valid_state = serializer.dumps({"nonce": secrets.token_urlsafe(16), "provider": "google"})

    with patch(
        "app.services.google_auth_service.exchange_code_for_id_token",
        side_effect=GoogleAuthError("Google token exchange failed (401)"),
    ):
        client = TestClient(app)
        res = client.post(
            "/api/v1/auth/google/callback",
            json={"code": "expired-code", "state": valid_state},
        )
        assert res.status_code == 401
        assert "Google token exchange failed" in res.json()["detail"]

