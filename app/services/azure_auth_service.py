"""Microsoft Entra ID (Azure AD) SSO (OAuth2/OIDC) and Graph services."""

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
import jwt
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import User
from app.services.user_service import UserProvisioningError, upsert_sso_user

logger = logging.getLogger(__name__)

AZURE_JWKS_CACHE_LIFESPAN = 3600  # 1 hour in-memory JWKS caching
AZURE_CLOCK_SKEW_LEEWAY_SECONDS = 10  # 10s tolerance for clock drift

_azure_jwks_clients: dict[str, jwt.PyJWKClient] = {}


class AzureAuthError(Exception):
    """Raised when Microsoft Entra ID authentication fails."""


def get_azure_jwks_client() -> jwt.PyJWKClient:
    """Retrieve or initialize the cached PyJWKClient for Azure AD."""
    tenant_id = settings.azure_tenant_id or "common"
    jwks_uri = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    client = _azure_jwks_clients.get(jwks_uri)
    if client is None:
        client = jwt.PyJWKClient(
            jwks_uri,
            cache_jwk_set=True,
            lifespan=AZURE_JWKS_CACHE_LIFESPAN,
            timeout=10.0,
        )
        _azure_jwks_clients[jwks_uri] = client
    return client


def clear_azure_jwks_cache() -> None:
    """Clear cached JWKS clients (primarily for testing)."""
    _azure_jwks_clients.clear()


def verify_azure_id_token(
    id_token: str,
    jwks_client: jwt.PyJWKClient | None = None,
) -> dict[str, Any]:
    """Verify cryptographic signature, issuer, audience, and expiration of an Azure ID token."""
    if not settings.azure_client_id:
        raise AzureAuthError("Microsoft Azure OAuth credentials are not configured")

    try:
        header = jwt.get_unverified_header(id_token)
        unverified_claims = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.PyJWTError as error:
        raise AzureAuthError(f"Malformed Azure ID token: {error}") from error

    alg = header.get("alg")
    if alg != "RS256":
        raise AzureAuthError(f"Unsupported Azure token algorithm: {alg}")

    kid = header.get("kid")
    if not kid:
        raise AzureAuthError("Missing kid header in Azure ID token")

    iss = unverified_claims.get("iss", "")
    tenant_id = settings.azure_tenant_id or "common"
    if tenant_id not in ("common", "organizations", "consumers"):
        expected_iss = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        if iss != expected_iss:
            raise AzureAuthError(f"Unexpected Azure token issuer: {iss}")
    else:
        tid = unverified_claims.get("tid")
        if not tid or iss != f"https://login.microsoftonline.com/{tid}/v2.0":
            raise AzureAuthError(f"Invalid Azure token issuer or tenant: {iss}")

    if unverified_claims.get("aud") != settings.azure_client_id:
        raise AzureAuthError("Azure token audience mismatch")

    now = datetime.now(UTC).timestamp()
    exp = unverified_claims.get("exp")
    if not exp or now > (float(exp) + AZURE_CLOCK_SKEW_LEEWAY_SECONDS):
        raise AzureAuthError("Azure token expired")

    client = jwks_client or get_azure_jwks_client()
    try:
        signing_key = client.get_signing_key_from_jwt(id_token)
    except Exception as error:
        logger.error("Failed to retrieve Azure signing key: %s", error)
        raise AzureAuthError(f"No matching Azure signing key: {error}") from error

    try:
        verified_claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.azure_client_id,
            leeway=AZURE_CLOCK_SKEW_LEEWAY_SECONDS,
        )
    except Exception as error:
        logger.error("Azure ID token signature verification failed: %s", error)
        raise AzureAuthError(f"Azure ID token signature check failed: {error}") from error

    return dict(verified_claims)


def azure_authorization_url(
    state: str, is_mobile: bool = False, redirect_uri: str | None = None
) -> str:
    """Build the Microsoft Entra ID authorization redirect URL."""
    if not settings.azure_client_id or not settings.azure_client_secret:
        raise AzureAuthError("Microsoft Azure OAuth credentials are not configured")
    tenant_id = settings.azure_tenant_id or "common"
    base_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"
    target_redirect_uri = redirect_uri or (
        settings.azure_mobile_redirect_uri
        if is_mobile and settings.azure_mobile_redirect_uri
        else settings.azure_redirect_uri
    )
    params = {
        "client_id": settings.azure_client_id,
        "response_type": "code",
        "redirect_uri": target_redirect_uri,
        "response_mode": "query",
        "scope": "openid profile email User.Read",
        "state": state,
    }
    query = "&".join(f"{key}={quote(str(val), safe='')}" for key, val in params.items())
    return f"{base_url}?{query}"


def exchange_code_for_azure_user(
    code: str,
    is_mobile: bool = False,
    redirect_uri: str | None = None,
    jwks_client: jwt.PyJWKClient | None = None,
) -> dict[str, Any]:
    """Exchange the authorization code for tokens and retrieve the Microsoft user profile."""
    if not settings.azure_client_id or not settings.azure_client_secret:
        raise AzureAuthError("Microsoft Azure OAuth credentials are not configured")
    tenant_id = settings.azure_tenant_id or "common"
    token_endpoint = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    target_redirect_uri = redirect_uri or (
        settings.azure_mobile_redirect_uri
        if is_mobile and settings.azure_mobile_redirect_uri
        else settings.azure_redirect_uri
    )

    try:
        response = httpx.post(
            token_endpoint,
            data={
                "client_id": settings.azure_client_id,
                "client_secret": settings.azure_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": target_redirect_uri,
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
        id_token_claims = verify_azure_id_token(id_token, jwks_client=jwks_client)

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
    try:
        return upsert_sso_user(
            session=session,
            provider="azure",
            provider_sub=str(user_info["azure_sub"]),
            email=str(user_info["email"]),
            name=user_info.get("name"),
            avatar_url=None,
        )
    except UserProvisioningError as error:
        raise AzureAuthError(str(error)) from error




