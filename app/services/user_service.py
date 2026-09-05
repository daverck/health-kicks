"""User management and JIT provisioning for SSO authentication.

Provides centralized user upsert, account linking across OAuth providers (Google,
Microsoft Entra ID), and atomic race-condition handling.
"""

from datetime import datetime, timezone
import logging
from typing import Literal
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import User, UserRole

logger = logging.getLogger(__name__)

SSOProvider = Literal["google", "azure"]


class UserProvisioningError(Exception):
    """Raised when JIT provisioning or user persistence fails."""


def upsert_sso_user(
    session: Session,
    provider: SSOProvider,
    provider_sub: str,
    email: str,
    name: str | None = None,
    avatar_url: str | None = None,
) -> User:
    """JIT-provision a user on SSO sign-in or link with an existing account by email.

    1. Checks if a user already exists with the given provider subject ID.
    2. If not, checks if an account already exists with the same email.
    3. If found, links the provider subject ID and refreshes metadata.
    4. If not found, creates a new User with the default user role.
    5. Safely handles concurrent logins via transaction rollback & re-query.
    """
    sub = str(provider_sub).strip()
    norm_email = str(email).strip().lower()

    if not norm_email:
        raise UserProvisioningError(f"{provider.capitalize()} account did not expose an email address")
    if not sub:
        raise UserProvisioningError(f"No valid user identifier found for {provider.capitalize()} account")

    db_target = session.get_bind().url.render_as_string(hide_password=True)
    logger.info("SSO sign-in [%s]: sub=%s email=%s db=%s", provider, sub, norm_email, db_target)

    def _find_user() -> User | None:
        if provider == "google":
            match = session.query(User).filter_by(google_sub=sub).one_or_none()
        elif provider == "azure":
            match = session.query(User).filter_by(azure_sub=sub).one_or_none()
        else:
            match = None

        if match is None:
            match = session.query(User).filter_by(email=norm_email).one_or_none()
            if match is not None:
                logger.info("SSO sign-in [%s]: matched existing user id=%s by email", provider, match.id)
        return match

    user = _find_user()
    if user is None:
        user = User(
            google_sub=sub if provider == "google" else None,
            azure_sub=sub if provider == "azure" else None,
            email=norm_email,
            name=name,
            avatar_url=avatar_url,
            role=UserRole.user,
        )
        session.add(user)
        logger.info("SSO sign-in [%s]: staging new user email=%s for insert", provider, norm_email)
    else:
        if provider == "google":
            user.google_sub = sub
        elif provider == "azure":
            user.azure_sub = sub

        if name:
            if not user.name or provider == "google":
                user.name = name
        if avatar_url:
            user.avatar_url = avatar_url

    user.last_login_utc = datetime.now(timezone.utc)

    try:
        session.commit()
    except IntegrityError as error:
        # Race: concurrent sign-in for same account. Roll back and re-read.
        session.rollback()
        logger.warning("SSO sign-in [%s]: concurrent insert for email=%s, re-reading", provider, norm_email)
        user = _find_user()
        if user is None:
            logger.error("SSO sign-in [%s]: insert failed and no user found: %s", provider, error)
            raise UserProvisioningError(f"User persistence failed: {error}") from error
    except SQLAlchemyError as error:
        session.rollback()
        logger.exception(
            "SSO sign-in [%s]: COMMIT FAILED against db=%s for email=%s — rolled back",
            provider,
            db_target,
            norm_email,
        )
        raise UserProvisioningError(f"Database error while persisting user: {error}") from error

    session.refresh(user)
    logger.info(
        "SSO sign-in [%s]: user persisted id=%s email=%s role=%s",
        provider,
        user.id,
        user.email,
        user.role.value,
    )
    return user
