"""Microsoft Entra ID (Azure AD) SSO (OAuth2/OIDC) and Graph services."""

from datetime import datetime, timezone
import logging
from typing import Any
from urllib.parse import quote

import httpx
import jwt
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import User, UserRole

logger = logging.getLogger(__name__)


class AzureAuthError(Exception):
    """Raised when Microsoft Entra ID authentication fails."""


def azure_authorization_url(state: str) -> str:
    """Build the Microsoft Entra ID authorization redirect URL."""
    if not settings.azure_client_id or not settings.azure_client_secret:
        raise AzureAuthError("Microsoft Azure OAuth credentials are not configured")
    tenant_id = settings.azure_tenant_id or "common"
    base_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"
    params = {
        "client_id": settings.azure_client_id,
        "response_type": "code",
        "redirect_uri": settings.azure_redirect_uri,
        "response_mode": "query",
        "scope": "openid profile email User.Read",
        "state": state,
    }
    query = "&".join(f"{key}={quote(str(val), safe='')}" for key, val in params.items())
    return f"{base_url}?{query}"


def exchange_code_for_azure_user(code: str) -> dict[str, Any]:
    """Exchange the authorization code for tokens and retrieve the Microsoft user profile."""
    if not settings.azure_client_id or not settings.azure_client_secret:
        raise AzureAuthError("Microsoft Azure OAuth credentials are not configured")
    tenant_id = settings.azure_tenant_id or "common"
    token_endpoint = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    try:
        response = httpx.post(
            token_endpoint,
            data={
                "client_id": settings.azure_client_id,
                "client_secret": settings.azure_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": settings.azure_redirect_uri,
            },
            timeout=10.0,
        )
    except httpx.HTTPError as error:
        logger.error("Azure token exchange HTTP error: %s", error)
        raise AzureAuthError(f"Azure token exchange failed: {error}") from error

    if response.status_code != 200:
        logger.error("Azure token endpoint error (%s): %s", response.status_code, response.text)
        raise AzureAuthError(f"Azure token exchange failed ({response.status_code})")

    data = response.json()
    id_token = data.get("id_token")
    access_token = data.get("access_token")

    id_token_claims: dict[str, Any] = {}
    if id_token:
        try:
            id_token_claims = jwt.decode(id_token, options={"verify_signature": False})
        except jwt.PyJWTError as error:
            logger.warning("Could not decode Azure ID token: %s", error)

    profile: dict[str, Any] = {}
    if access_token:
        try:
            graph_res = httpx.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10.0,
            )
            if graph_res.status_code == 200:
                profile = graph_res.json()
            else:
                logger.warning(
                    "Microsoft Graph /me returned %s: %s",
                    graph_res.status_code,
                    graph_res.text,
                )
        except httpx.HTTPError as error:
            logger.warning("Microsoft Graph /me request failed: %s", error)

    # Required email resolution fallback order:
    # 1. profile['mail']
    # 2. profile['userPrincipalName']
    # 3. id_token_claims['email']
    # 4. id_token_claims['preferred_username']
    raw_email = (
        profile.get("mail")
        or profile.get("userPrincipalName")
        or id_token_claims.get("email")
        or id_token_claims.get("preferred_username")
    )
    if not raw_email or not str(raw_email).strip():
        raise AzureAuthError("No valid email found in Microsoft account")
    email = str(raw_email).strip().lower()

    azure_sub = str(
        profile.get("id")
        or id_token_claims.get("sub")
        or id_token_claims.get("oid")
        or ""
    ).strip()
    if not azure_sub:
        raise AzureAuthError("No valid user identifier found in Microsoft account")

    name = (
        profile.get("displayName")
        or id_token_claims.get("name")
        or f"{profile.get('givenName', '')} {profile.get('surname', '')}".strip()
        or None
    )

    return {
        "azure_sub": azure_sub,
        "email": email,
        "name": name,
        "profile": profile,
        "id_token_claims": id_token_claims,
    }


def get_or_create_azure_user(session: Session, user_info: dict[str, Any]) -> User:
    """JIT-provision the user on Azure sign-in or link with existing email account."""
    azure_sub = str(user_info["azure_sub"])
    email = str(user_info["email"]).strip().lower()
    name = user_info.get("name")

    db_target = session.get_bind().url.render_as_string(hide_password=True)
    logger.info("Azure SSO sign-in: azure_sub=%s email=%s db=%s", azure_sub, email, db_target)

    user = session.query(User).filter_by(azure_sub=azure_sub).one_or_none()
    if user is None:
        user = session.query(User).filter_by(email=email).one_or_none()
        if user is not None:
            logger.info("Azure SSO sign-in: matched existing user id=%s by email", user.id)

    if user is None:
        user = User(
            azure_sub=azure_sub,
            email=email,
            name=name,
            role=UserRole.user,
        )
        session.add(user)
        logger.info("Azure SSO sign-in: staging new user email=%s for insert", email)
    else:
        user.azure_sub = azure_sub
        if not user.name and name:
            user.name = name

    user.last_login_utc = datetime.now(timezone.utc)

    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        logger.warning("Azure SSO sign-in: concurrent insert for email=%s, re-reading", email)
        user = (
            session.query(User).filter_by(azure_sub=azure_sub).one_or_none()
            or session.query(User).filter_by(email=email).one_or_none()
        )
        if user is None:
            logger.error("Azure SSO sign-in: insert failed and no user found: %s", error)
            raise AzureAuthError(f"User persistence failed: {error}") from error
    except SQLAlchemyError as error:
        session.rollback()
        logger.exception(
            "Azure SSO sign-in: COMMIT FAILED against db=%s for email=%s — rolled back",
            db_target,
            email,
        )
        raise AzureAuthError(f"Database error while persisting user: {error}") from error

    session.refresh(user)
    logger.info(
        "Azure SSO sign-in: user persisted id=%s email=%s role=%s",
        user.id,
        user.email,
        user.role.value,
    )
    return user



