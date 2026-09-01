"""Google SSO (OAuth2/OIDC) and JWT access-token services.

The API is stateless: users authenticate through Google's Authorization Code
flow, the ID token is verified against Google's JWKS, the account is
auto-provisioned on first sign-in (JIT), and the API issues its own signed
JWT access token for subsequent requests.
"""

from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any

import jwt
import httpx
from jwt.algorithms import RSAAlgorithm
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.core.config import settings
from app.db.models import User, UserRole

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}


class GoogleAuthError(Exception):
    """Raised when the Google ID token cannot be trusted."""


def google_authorization_url(state: str) -> str:
    """Build the Google consent redirect (Authorization Code + OIDC scopes)."""
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.oauth_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    query = "&".join(f"{key}={_urlencode(value)}" for key, value in params.items())
    return f"{GOOGLE_AUTH_ENDPOINT}?{query}"


def _urlencode(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def exchange_code_for_id_token(code: str) -> dict[str, Any]:
    """Exchange the authorization code and verify the returned Google ID token."""
    if not settings.google_client_id or not settings.google_client_secret:
        raise GoogleAuthError("Google OAuth credentials are not configured")
    response = httpx.post(
        GOOGLE_TOKEN_ENDPOINT,
        data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.oauth_redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=10.0,
    )
    if response.status_code != 200:
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

    # Marge de tolérance de 10 secondes pour éviter l'erreur de décalage d'horloge
    leeway = 10
    now = datetime.now(timezone.utc).timestamp()
    exp = payload.get("exp")
    if not exp or now > (float(exp) + leeway):
        raise GoogleAuthError("Google token expired")
    exp = payload.get("exp")

    jwks = _google_jwks()
    key = _find_key(jwks, header.get("kid"))
    if key is None:
        raise GoogleAuthError("No matching Google signing key")
    try:
        public_key = RSAAlgorithm.from_jwk(json.dumps(key))
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


def _google_jwks() -> dict[str, Any]:
    response = httpx.get(GOOGLE_JWKS_URI, timeout=10.0)
    response.raise_for_status()
    return response.json()


def _find_key(jwks: dict[str, Any], kid: str | None) -> dict[str, Any] | None:
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


def get_or_create_user(session: Session, claims: dict[str, Any]) -> User:
    """JIT-provision the user on first Google sign-in, then refresh presence.

    Every step is logged so that a missing row in the ``users`` table can be
    traced from the Docker logs (``docker compose logs api``).
    """
    google_sub = str(claims["sub"])
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise GoogleAuthError("Google account did not expose an email address")

    db_target = session.get_bind().url.render_as_string(hide_password=True)
    logger.info("SSO sign-in: sub=%s email=%s db=%s", google_sub, email, db_target)

    user = session.query(User).filter_by(google_sub=google_sub).one_or_none()
    if user is None:
        user = session.query(User).filter_by(email=email).one_or_none()
        if user is not None:
            logger.info("SSO sign-in: matched existing user id=%s by email", user.id)
    if user is None:
        user = User(
            google_sub=google_sub,
            email=email,
            name=claims.get("name"),
            avatar_url=claims.get("picture"),
            role=UserRole.user,
        )
        session.add(user)
        logger.info("SSO sign-in: staging new user email=%s for insert", email)
    else:
        user.google_sub = google_sub
        user.name = claims.get("name") or user.name
        user.avatar_url = claims.get("picture") or user.avatar_url
    user.last_login_utc = datetime.now(timezone.utc)

    try:
        session.commit()
    except IntegrityError as error:
        # Race: the same Google account signed in concurrently. Roll back and
        # re-read the row the other request inserted instead of failing.
        session.rollback()
        logger.warning("SSO sign-in: concurrent insert for email=%s, re-reading", email)
        user = (
            session.query(User).filter_by(google_sub=google_sub).one_or_none()
            or session.query(User).filter_by(email=email).one_or_none()
        )
        if user is None:
            logger.error("SSO sign-in: insert failed and no user found: %s", error)
            raise GoogleAuthError(f"User persistence failed: {error}") from error
    except SQLAlchemyError as error:
        session.rollback()
        logger.exception(
            "SSO sign-in: COMMIT FAILED against db=%s for email=%s — rolled back",
            db_target,
            email,
        )
        raise GoogleAuthError(f"Database error while persisting user: {error}") from error

    session.refresh(user)
    logger.info(
        "SSO sign-in: user persisted id=%s email=%s role=%s",
        user.id,
        user.email,
        user.role.value,
    )
    return user


def issue_access_token(user: User) -> str:
    """Sign a stateless access token for API calls."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "google_sub": user.google_sub,
        "email": user.email,
        "role": user.role.value,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_access_token(token: str) -> dict[str, Any]:
    """Verify one of our own access tokens, raising jwt.PyJWTError on failure."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
