"""Unit tests for user_service (SSO JIT provisioning and account linking)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, User, UserRole
from app.services.user_service import UserProvisioningError, upsert_sso_user


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_upsert_sso_user_empty_email(db_session):
    with pytest.raises(UserProvisioningError, match="did not expose an email"):
        upsert_sso_user(db_session, "google", "sub-1", "")


def test_upsert_sso_user_empty_sub(db_session):
    with pytest.raises(UserProvisioningError, match="No valid user identifier"):
        upsert_sso_user(db_session, "google", "", "user@example.com")


def test_upsert_sso_user_creates_new(db_session):
    user = upsert_sso_user(
        db_session,
        provider="google",
        provider_sub="g-101",
        email="NewUser@example.com",
        name="New User",
        avatar_url="https://example.com/avatar.png",
    )
    assert user.id is not None
    assert user.google_sub == "g-101"
    assert user.azure_sub is None
    assert user.email == "newuser@example.com"
    assert user.name == "New User"
    assert user.avatar_url == "https://example.com/avatar.png"
    assert user.role == UserRole.user
    assert user.last_login_utc is not None


def test_upsert_sso_user_links_existing_by_email(db_session):
    # Existing user registered via Google
    existing = User(
        google_sub="g-202",
        email="common@example.com",
        name="Common User",
        role=UserRole.admin,
    )
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)

    # Now signs in via Azure with same email
    linked = upsert_sso_user(
        db_session,
        provider="azure",
        provider_sub="az-202",
        email="Common@Example.COM",
        name="Common User Azure",
    )

    assert linked.id == existing.id
    assert linked.google_sub == "g-202"
    assert linked.azure_sub == "az-202"
    assert linked.role == UserRole.admin

