"""GDPR and HIPAA compliant deterministic pseudonymization service.

Implements cryptographic pseudonymization using HMAC-SHA256 with an environment-level
secret pepper to ensure pseudonymized identifiers (e.g. 'SUBJ-A8F19C2D') cannot be reversed
without access to the application secret keys (Article 4(5) GDPR).
"""

import hashlib
import hmac
from typing import Any

from app.core.config import get_settings


def _get_pepper(pepper: str | None = None) -> bytes:
    """Retrieve the active cryptographic pepper as bytes."""
    secret = pepper or get_settings().pseudonymization_pepper
    return secret.encode("utf-8")


def get_subject_pseudonym(user_id: int | str | Any, pepper: str | None = None) -> str:
    """Generate a deterministic pseudonymized subject identifier (e.g. 'SUBJ-A8F19C2D').

    Stable across queries for cohort analysis, while preventing re-identification without the pepper.
    """
    secret = _get_pepper(pepper)
    raw_id = str(user_id).strip()
    digest = hmac.new(secret, raw_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"SUBJ-{digest[:8].upper()}"


def get_device_pseudonym(device_id: str, pepper: str | None = None) -> str:
    """Generate a deterministic pseudonymized device identifier (e.g. 'DEV-3E7F9B1A')."""
    secret = _get_pepper(pepper)
    raw_dev = str(device_id).strip()
    digest = hmac.new(secret, raw_dev.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"DEV-{digest[:8].upper()}"


def mask_email(email: str | None) -> str | None:
    """Mask email address for privacy preserving projections (e.g. 'j***@d***.com')."""
    if not email or "@" not in email:
        return None
    local, domain = email.split("@", 1)
    masked_local = f"{local[0]}***" if local else "***"
    domain_parts = domain.split(".", 1)
    if len(domain_parts) == 2:
        masked_domain = f"{domain_parts[0][0]}***.{domain_parts[1]}"
    else:
        masked_domain = f"{domain[0]}***"
    return f"{masked_local}@{masked_domain}"

