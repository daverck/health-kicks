"""Tests for Google SSO auth service, security dependencies and protected routes."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_current_user, require_roles
from app.db.models import Base, User, UserRole
from app.services import auth_service


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_issue_and_verify_access_token_roundtrip(db_session) -> None:
    user = User(google_sub="sub-1", email="a@example.com", role=UserRole.clinician)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    token = auth_service.issue_access_token(user)
    claims = auth_service.verify_access_token(token)
    assert claims["sub"] == str(user.id)
    assert claims["role"] == "clinician"
    assert claims["email"] == "a@example.com"
    assert claims["exp"] > claims["iat"]


def test_verify_rejects_tampered_token() -> None:

    token = auth_service.issue_access_token(
        User(google_sub="s", email="b@example.com", role=UserRole.user)
    )
    with pytest.raises(Exception):
        auth_service.verify_access_token(token + "x")


def test_get_or_create_user_jit_provisions_once(db_session) -> None:
    claims = {"sub": "g-1", "email": "New@Example.com", "name": "New User", "picture": "p"}
    first = auth_service.get_or_create_user(db_session, claims)
    second = auth_service.get_or_create_user(db_session, claims)
    assert first.id == second.id
    assert first.email == "new@example.com"
    assert first.role == UserRole.user
    assert first.last_login_utc is not None


def test_get_or_create_user_rebinds_existing_email(db_session) -> None:
    existing = User(google_sub="old-sub", email="x@example.com", role=UserRole.admin)
    db_session.add(existing)
    db_session.commit()
    user = auth_service.get_or_create_user(db_session, {"sub": "new-sub", "email": "x@example.com"})
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
    token = auth_service.issue_access_token(user)

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
    admin_token = auth_service.issue_access_token(admin)

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
