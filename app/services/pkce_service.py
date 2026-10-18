"""PKCE (RFC 7636) and anti-CSRF OAuth state management service."""

import base64
import hashlib
import logging
import secrets
from typing import Any

from fastapi import HTTPException
from itsdangerous import BadData, SignatureExpired, URLSafeTimedSerializer

from app.core.config import Settings, settings

logger = logging.getLogger(__name__)


def get_timed_serializer(config: Settings = settings) -> URLSafeTimedSerializer:
    """Return a URLSafeTimedSerializer using the configured JWT secret."""
    return URLSafeTimedSerializer(config.jwt_secret, salt="oauth-state")


def generate_oauth_state(
    provider: str,
    platform: str = "web",
    code_challenge: str | None = None,
    config: Settings = settings,
) -> str:
    """Generate a signed anti-CSRF OAuth state containing a nonce, provider name, platform, and optional PKCE challenge."""
    serializer = get_timed_serializer(config)
    payload: dict[str, Any] = {
        "platform": platform,
        "provider": provider,
        "nonce": secrets.token_hex(16),
    }
    if code_challenge is not None:
        payload["cc"] = code_challenge
    return serializer.dumps(payload)


def verify_oauth_state(
    state: str,
    expected_provider: str,
    max_age: int = 600,
    config: Settings = settings,
) -> dict[str, Any]:
    """Validate the cryptographic signature, expiration (max_age seconds), and provider."""
    serializer = get_timed_serializer(config)
    try:
        data = serializer.loads(state, max_age=max_age)
    except SignatureExpired as error:
        logger.warning("Expired OAuth state token: %s", error)
        raise HTTPException(status_code=400, detail="Invalid OAuth state: state expired") from error
    except BadData as error:
        logger.warning("Invalid OAuth state signature: %s", error)
        raise HTTPException(status_code=400, detail="Invalid OAuth state") from error

    if not isinstance(data, dict) or data.get("provider") != expected_provider:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    return data


def verify_pkce_challenge(code_challenge: str | None, code_verifier: str | None) -> None:
    """Verify S256 PKCE code_verifier against the stored code_challenge when present."""
    if not code_challenge:
        return
    if not code_verifier:
        raise HTTPException(status_code=400, detail="Missing PKCE code_verifier")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    if not secrets.compare_digest(computed, code_challenge):
        raise HTTPException(status_code=400, detail="Invalid PKCE code_verifier")


def get_state_platform(state: str | None, config: Settings = settings) -> str:
    """Extract the target platform ('mobile' or 'web') from the state payload without enforcing strict signature/expiry."""
    if not state:
        return "web"
    try:
        serializer = get_timed_serializer(config)
        _, payload = serializer.loads_unsafe(state)
        if isinstance(payload, dict) and "platform" in payload:
            return str(payload["platform"])
    except Exception:
        pass
    return "web"

