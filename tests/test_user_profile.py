"""Tests for UserUpdate schema and PATCH /api/v1/users/{user_id} endpoint.

Validates that modifying user email is strictly forbidden to preserve OIDC IdP consistency.
"""

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import get_db
from app.db.models import Base, User, UserRole
from app.main import app
from app.schemas.user import UserUpdate
from app.services import token_service


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
def admin_user(db_session) -> User:
    admin = User(
        google_sub="admin-sub",
        email="admin@healthkicks.org",
        name="Admin User",
        role=UserRole.admin,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin


@pytest.fixture()
def regular_user(db_session) -> User:
    user = User(
        google_sub="patient-sub",
        email="patient@healthkicks.org",
        name="John Patient",
        role=UserRole.user,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def client(db_session) -> TestClient:
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Schema Unit Tests
# ---------------------------------------------------------------------------


def test_user_update_schema_accepts_valid_fields() -> None:
    update = UserUpdate(role=UserRole.clinician, is_active=False)
    assert update.role == UserRole.clinician
    assert update.is_active is False


def test_user_update_schema_rejects_email() -> None:
    with pytest.raises(ValidationError) as exc_info:
        UserUpdate.model_validate({"email": "hacked@example.com"})
    errors = exc_info.value.errors()
    assert any("Email address cannot be modified" in str(e["msg"]) for e in errors)


def test_user_update_schema_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError) as exc_info:
        UserUpdate.model_validate({"full_name": "New Name"})
    errors = exc_info.value.errors()
    assert any(e["type"] == "extra_forbidden" for e in errors)


# ---------------------------------------------------------------------------
# API Route Tests (PATCH /api/v1/users/{user_id})
# ---------------------------------------------------------------------------


def test_admin_updates_user_role_and_status_successfully(
    client, admin_user, regular_user, db_session
) -> None:
    token = token_service.issue_access_token(admin_user)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.patch(
        f"/api/v1/users/{regular_user.id}",
        headers=headers,
        json={"role": "clinician", "is_active": False},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == regular_user.id
    assert data["role"] == "clinician"
    assert data["is_active"] is False
    assert data["email"] == "patient@healthkicks.org"

    # Verify directly in database
    db_session.refresh(regular_user)
    assert regular_user.role == UserRole.clinician
    assert regular_user.is_active is False
    assert regular_user.email == "patient@healthkicks.org"


def test_admin_cannot_update_user_email(client, admin_user, regular_user, db_session) -> None:
    token = token_service.issue_access_token(admin_user)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.patch(
        f"/api/v1/users/{regular_user.id}",
        headers=headers,
        json={"email": "attacker@evil.com"},
    )

    # Must be rejected with 422 Unprocessable Entity
    assert response.status_code == 422
    assert "Email address cannot be modified" in str(response.json())

    # Verify email in DB remains strictly unchanged
    db_session.refresh(regular_user)
    assert regular_user.email == "patient@healthkicks.org"


def test_admin_cannot_update_user_email_even_with_valid_fields(
    client, admin_user, regular_user, db_session
) -> None:
    token = token_service.issue_access_token(admin_user)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.patch(
        f"/api/v1/users/{regular_user.id}",
        headers=headers,
        json={"role": "clinician", "email": "attacker@evil.com"},
    )

    assert response.status_code == 422
    db_session.refresh(regular_user)
    assert regular_user.role == UserRole.user  # Not updated
    assert regular_user.email == "patient@healthkicks.org"


def test_unauthenticated_request_rejected(client, regular_user) -> None:
    response = client.patch(
        f"/api/v1/users/{regular_user.id}",
        json={"role": "clinician"},
    )
    assert response.status_code == 401


def test_non_admin_cannot_update_user(client, regular_user) -> None:
    token = token_service.issue_access_token(regular_user)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.patch(
        f"/api/v1/users/{regular_user.id}",
        headers=headers,
        json={"role": "admin"},
    )
    assert response.status_code == 403


def test_update_nonexistent_user_returns_404(client, admin_user) -> None:
    token = token_service.issue_access_token(admin_user)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.patch(
        "/api/v1/users/99999",
        headers=headers,
        json={"role": "clinician"},
    )
    assert response.status_code == 404
