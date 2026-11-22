import uuid

"""Tests for UserUpdate schema and PATCH /api/v1/users/{user_id} endpoint.



Validates that modifying user email is strictly forbidden to preserve OIDC IdP consistency.

"""


import pytest
from fastapi.testclient import TestClient
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

    update = UserUpdate(role=UserRole.trainer, is_active=False)

    assert update.role == UserRole.trainer

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
        f"/api/v1/users/{regular_user.uuid}",
        headers=headers,
        json={"role": "trainer", "is_active": False},
    )

    assert response.status_code == 200

    data = response.json()

    assert "id" not in data

    assert data["uuid"] == str(regular_user.uuid)

    assert data["pseudonym"] == regular_user.pseudonym

    assert data["role"] == "trainer"

    assert data["is_active"] is False

    assert data["email"] == "patient@healthkicks.org"

    # Verify directly in database

    db_session.refresh(regular_user)

    assert regular_user.role == UserRole.trainer

    assert regular_user.is_active is False

    assert regular_user.email == "patient@healthkicks.org"


def test_admin_cannot_update_user_email(client, admin_user, regular_user, db_session) -> None:

    token = token_service.issue_access_token(admin_user)

    headers = {"Authorization": f"Bearer {token}"}

    response = client.patch(
        f"/api/v1/users/{regular_user.uuid}",
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
        f"/api/v1/users/{regular_user.uuid}",
        headers=headers,
        json={"role": "trainer", "email": "attacker@evil.com"},
    )

    assert response.status_code == 422

    db_session.refresh(regular_user)

    assert regular_user.role == UserRole.user  # Not updated

    assert regular_user.email == "patient@healthkicks.org"


def test_unauthenticated_request_rejected(client, regular_user) -> None:

    response = client.patch(
        f"/api/v1/users/{regular_user.uuid}",
        json={"role": "trainer"},
    )

    assert response.status_code == 401


def test_non_admin_cannot_update_user(client, regular_user) -> None:

    token = token_service.issue_access_token(regular_user)

    headers = {"Authorization": f"Bearer {token}"}

    response = client.patch(
        f"/api/v1/users/{regular_user.uuid}",
        headers=headers,
        json={"role": "admin"},
    )

    assert response.status_code == 403


def test_update_nonexistent_user_returns_404(client, admin_user) -> None:

    token = token_service.issue_access_token(admin_user)

    headers = {"Authorization": f"Bearer {token}"}

    response = client.patch(
        f"/api/v1/users/{uuid.uuid4()}",
        headers=headers,
        json={"role": "trainer"},
    )

    assert response.status_code == 404


def test_user_self_deletion_with_anonymization_rekeying(
    client, regular_user, db_session
) -> None:
    """User self-deletes via DELETE /api/v1/users/me:
    - Returns 204 No Content.
    - Subsequent requests with that user's token return 401.
    - User's studio sessions persist with user_id is None and author_uuid != original_user.uuid.
    - All studio sessions of that user share the new author_uuid.
    - Alert contacts and device ownership records are deleted.
    """
    from app.db.models import AlertContact, DeviceOwnership, StudioSession

    token = token_service.issue_access_token(regular_user)
    headers = {"Authorization": f"Bearer {token}"}
    old_user_uuid = regular_user.uuid
    old_user_id = regular_user.id

    # Seed related entities
    s1 = StudioSession(
        uuid=uuid.uuid4(),
        user_id=old_user_id,
        author_uuid=old_user_uuid,
        device_id="HK-1",
        label="walk",
    )
    s2 = StudioSession(
        uuid=uuid.uuid4(),
        user_id=old_user_id,
        author_uuid=old_user_uuid,
        device_id="HK-1",
        label="run",
    )
    contact = AlertContact(
        user_id=old_user_id,
        first_name="Jane",
        last_name="Doe",
        email="jane.doe@example.com",
        priority=1,
    )
    ownership = DeviceOwnership(
        user_id=old_user_id,
        device_id="HK-1",
    )
    db_session.add_all([s1, s2, contact, ownership])
    db_session.commit()

    # Self delete
    res = client.delete("/api/v1/users/me", headers=headers)
    assert res.status_code == 204
    assert res.text == ""

    # Subsequent request with old token returns 401 Unauthorized
    res_subsequent = client.get("/api/v1/users", headers=headers)
    assert res_subsequent.status_code == 401

    # Verify user record is deleted
    assert db_session.query(User).filter(User.id == old_user_id).one_or_none() is None

    # Verify studio sessions are retained with user_id = None and blinded author_uuid
    sessions = (
        db_session.query(StudioSession)
        .filter(StudioSession.uuid.in_([s1.uuid, s2.uuid]))
        .all()
    )
    assert len(sessions) == 2
    for s in sessions:
        assert s.user_id is None
        assert s.author_uuid is not None
        assert s.author_uuid != old_user_uuid

    # Both sessions share the exact same re-keyed cohort UUID
    assert sessions[0].author_uuid == sessions[1].author_uuid

    # Verify cascading deletion of alert contacts and device ownership
    assert (
        db_session.query(AlertContact).filter(AlertContact.user_id == old_user_id).count() == 0
    )
    assert (
        db_session.query(DeviceOwnership).filter(DeviceOwnership.user_id == old_user_id).count()
        == 0
    )


def test_admin_can_delete_another_user_by_uuid(
    client, admin_user, regular_user, db_session
) -> None:
    """Administrator can delete another user via DELETE /api/v1/users/{uuid} -> 204."""
    admin_token = token_service.issue_access_token(admin_user)
    headers = {"Authorization": f"Bearer {admin_token}"}
    target_uuid = regular_user.uuid

    res = client.delete(f"/api/v1/users/{target_uuid}", headers=headers)
    assert res.status_code == 204
    assert db_session.query(User).filter(User.uuid == target_uuid).one_or_none() is None


def test_non_admin_cannot_delete_another_user_by_uuid(
    client, regular_user, db_session
) -> None:
    """Non-administrator attempting to delete another user receives 403 Forbidden."""
    victim = User(
        google_sub="victim-sub",
        email="victim@healthkicks.org",
        name="Victim User",
        role=UserRole.user,
        is_active=True,
    )
    db_session.add(victim)
    db_session.commit()
    db_session.refresh(victim)

    token = token_service.issue_access_token(regular_user)
    headers = {"Authorization": f"Bearer {token}"}

    res = client.delete(f"/api/v1/users/{victim.uuid}", headers=headers)
    assert res.status_code == 403
    assert "Forbidden" in res.json()["detail"]
    assert db_session.query(User).filter(User.uuid == victim.uuid).one_or_none() is not None


def test_delete_nonexistent_user_returns_404(client, admin_user) -> None:
    """Admin deleting an unknown user UUID returns 404 Not Found."""
    admin_token = token_service.issue_access_token(admin_user)
    headers = {"Authorization": f"Bearer {admin_token}"}

    res = client.delete(f"/api/v1/users/{uuid.uuid4()}", headers=headers)
    assert res.status_code == 404
