"""Google SSO (OAuth2/OIDC) services.

The API is stateless: users authenticate through Google's Authorization Code
flow, the ID token is verified against Google's JWKS, and the account is
auto-provisioned on first sign-in (JIT).
"""

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import User
from app.services.user_service import UserProvisioningError, upsert_sso_user

logger = logging.getLogger(__name__)

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
GOOGLE_JWKS_CACHE_TTL_SEC = 3600

_cached_google_jwks: dict[str, Any] | None = None
_cached_google_jwks_expires_at: float = 0.0
_ORIG_HTTPX_GET = httpx.get
_ORIG_HTTPX_POST = httpx.post
_http_client: httpx.Client | None = None


def _get_http_client() -> httpx.Client:
    """Return a shared persistent HTTP client for Google OAuth/JWKS requests."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.Client(timeout=10.0)
    return _http_client


def _http_post(url: str, **kwargs: Any) -> httpx.Response:
    if httpx.post is not _ORIG_HTTPX_POST:
        return httpx.post(url, timeout=10.0, **kwargs)
    return _get_http_client().post(url, **kwargs)


def _http_get(url: str, **kwargs: Any) -> httpx.Response:
    if httpx.get is not _ORIG_HTTPX_GET:
        return httpx.get(url, timeout=10.0, **kwargs)
    return _get_http_client().get(url, **kwargs)


def clear_google_jwks_cache() -> None:
    """Clear in-memory cached Google JWKS certificates."""
    global _cached_google_jwks, _cached_google_jwks_expires_at
    _cached_google_jwks = None
    _cached_google_jwks_expires_at = 0.0


class GoogleAuthError(Exception):
    """Raised when the Google ID token cannot be trusted."""


def google_authorization_url(
    state: str, is_mobile: bool = False, redirect_uri: str | None = None
) -> str:
    """Build the Google consent redirect (Authorization Code + OIDC scopes)."""
    target_redirect_uri = redirect_uri or (
        settings.google_mobile_redirect_uri
        if is_mobile and settings.google_mobile_redirect_uri
        else settings.google_redirect_uri
    )
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": target_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    query = "&".join(f"{key}={quote(str(value), safe='')}" for key, value in params.items())
    return f"{GOOGLE_AUTH_ENDPOINT}?{query}"


def exchange_code_for_id_token(
    code: str, is_mobile: bool = False, redirect_uri: str | None = None
) -> dict[str, Any]:
    """Exchange the authorization code and verify the returned Google ID token."""
    if not settings.google_client_id or not settings.google_client_secret:
        raise GoogleAuthError("Google OAuth credentials are not configured")
    target_redirect_uri = redirect_uri or (
        settings.google_mobile_redirect_uri
        if is_mobile and settings.google_mobile_redirect_uri
        else settings.google_redirect_uri
    )
    try:
        response = _http_post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": target_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    except httpx.HTTPError as error:
        logger.error("Google token exchange HTTP error: %s", error)
        raise GoogleAuthError(f"Google token exchange network failure: {error}") from error

    if response.status_code != 200:
        logger.error("Google token endpoint error (%s): %s", response.status_code, response.text)
        raise GoogleAuthError(f"Google token exchange failed ({response.status_code})")
    id_token = response.json().get("id_token")
    if not id_token:
        raise GoogleAuthError("Google did not return an id_token")
    return verify_google_id_token(id_token)


def verify_google_id_token(id_token: str) -> dict[str, Any]:
    """Verify signature/issuer/audience/expiry of a Google ID token via JWKS."""
    try:
        header = jwt.get_unverified_header(id_token)
        payload = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.PyJWTError as error:
        raise GoogleAuthError(f"Malformed Google ID token: {error}") from error

    if payload.get("iss") not in GOOGLE_ISSUERS:
        raise GoogleAuthError("Unexpected Google token issuer")
    if payload.get("aud") != settings.google_client_id:
        raise GoogleAuthError("Google token audience mismatch")

    # 10-second clock skew tolerance leeway
    leeway = 10
    now = datetime.now(UTC).timestamp()
    exp = payload.get("exp")
    if not exp or now > (float(exp) + leeway):
        raise GoogleAuthError("Google token expired")

    was_cached = _cached_google_jwks is not None and time.monotonic() < _cached_google_jwks_expires_at
    jwks = _google_jwks()
    key = _find_key(jwks, header.get("kid"))
    if key is None and was_cached:
        clear_google_jwks_cache()
        jwks = _google_jwks()
        key = _find_key(jwks, header.get("kid"))
    if key is None:
        raise GoogleAuthError("No matching Google signing key")
    try:
        public_key: Any = RSAAlgorithm.from_jwk(json.dumps(key))
        # Signature + exp check (10 s leeway); iss/aud already validated above.
        jwt.decode(
            id_token,
            public_key,
            algorithms=[header.get("alg", "RS256")],
            audience=settings.google_client_id,
            leeway=leeway,
        )
    except Exception as error:
        raise GoogleAuthError(f"Google ID token signature check failed: {error}") from error
    return dict(payload)


def _google_jwks(force_refresh: bool = False) -> dict[str, Any]:
    global _cached_google_jwks, _cached_google_jwks_expires_at
    now = time.monotonic()
    if not force_refresh and _cached_google_jwks is not None and now < _cached_google_jwks_expires_at:
        return _cached_google_jwks
    try:
        response = _http_get(GOOGLE_JWKS_URI)
        response.raise_for_status()
        jwks_data = response.json()
        if isinstance(jwks_data, dict) and jwks_data.get("keys"):
            _cached_google_jwks = jwks_data
            _cached_google_jwks_expires_at = now + GOOGLE_JWKS_CACHE_TTL_SEC
        return jwks_data
    except httpx.HTTPError as error:
        logger.error("Failed to retrieve Google JWKS certs: %s", error)
        raise GoogleAuthError(f"Failed to retrieve Google signing keys: {error}") from error


def _find_key(jwks: dict[str, Any], kid: str | None) -> dict[str, Any] | None:
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


def get_or_create_user(session: Session, claims: dict[str, Any]) -> User:
    """JIT-provision the user on first Google sign-in, then refresh presence."""
    try:
        return upsert_sso_user(
            session=session,
            provider="google",
            provider_sub=str(claims.get("sub") or ""),
            email=str(claims.get("email") or ""),
            name=claims.get("name"),
            avatar_url=claims.get("picture"),
        )
    except UserProvisioningError as error:
        raise GoogleAuthError(str(error)) from error
