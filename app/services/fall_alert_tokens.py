"""Token generation, verification, and privacy formatting helpers for fall alerts."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import Settings, get_settings

ACK_TOKEN_SALT = "healthkicks-fall-alert-ack-v1"


def _resolve_settings(config: Settings | None = None) -> Settings:
    if config is not None:
        return config
    svc = sys.modules.get("app.services.fall_alert_service")
    if svc is not None and hasattr(svc, "get_settings"):
        return svc.get_settings()
    return get_settings()


class AckTokenError(Exception):
    """Raised when a fall alert acknowledgment token is invalid or expired."""

    def __init__(self, message: str, *, expired: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.expired = expired


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _iso_utc(dt: datetime | None) -> str | None:
    norm = _ensure_utc(dt)
    if norm is None:
        return None
    return norm.isoformat().replace("+00:00", "Z")


def _mask_last_name(last_name: str | None) -> str:
    if not last_name:
        return ""
    cleaned = last_name.strip()
    return f"{cleaned[0].upper()}." if cleaned else ""


def _mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    cleaned = phone.strip()
    if not cleaned:
        return None
    if len(cleaned) <= 4:
        return "*" * len(cleaned)
    prefix_len = 3 if cleaned.startswith("+") else 2
    if len(cleaned) <= prefix_len + 2:
        return cleaned[:1] + "*" * (len(cleaned) - 2) + cleaned[-1:]
    return f"{cleaned[:prefix_len]}{'*' * (len(cleaned) - prefix_len - 2)}{cleaned[-2:]}"


def generate_ack_token(
    alert_id: int,
    notification_id: int,
    config: Settings | None = None,
) -> str:
    """Create an HMAC-signed timed token for acknowledging a fall alert."""
    cfg = _resolve_settings(config)
    serializer = URLSafeTimedSerializer(
        secret_key=cfg.fall_alert_ack_secret,
        salt=ACK_TOKEN_SALT,
    )
    return str(serializer.dumps({"a": int(alert_id), "n": int(notification_id)}))


def verify_ack_token(
    token: str,
    config: Settings | None = None,
) -> tuple[int, int]:
    """Verify signature and TTL of an acknowledgment token, returning (alert_id, notification_id)."""
    if not token or not token.strip():
        raise AckTokenError("Jeton de prise en charge manquant.", expired=False)
    cfg = _resolve_settings(config)
    serializer = URLSafeTimedSerializer(
        secret_key=cfg.fall_alert_ack_secret,
        salt=ACK_TOKEN_SALT,
    )
    try:
        data = serializer.loads(token.strip(), max_age=cfg.fall_alert_ack_token_ttl_sec)
    except SignatureExpired as exc:
        raise AckTokenError(
            "Ce lien de prise en charge a expiré (validité maximale : 2 heures).",
            expired=True,
        ) from exc
    except BadSignature as exc:
        raise AckTokenError(
            "Ce lien de prise en charge est invalide ou altéré.",
            expired=False,
        ) from exc

    if not isinstance(data, dict) or "a" not in data or "n" not in data:
        raise AckTokenError("Structure de jeton invalide.", expired=False)
    try:
        return int(data["a"]), int(data["n"])
    except (TypeError, ValueError) as exc:
        raise AckTokenError("Identifiants de jeton invalides.", expired=False) from exc


def build_ack_url(
    alert_id: int,
    notification_id: int,
    config: Settings | None = None,
) -> str:
    """Build the full public URL for the emergency contact acknowledgment page on the frontend."""
    cfg = _resolve_settings(config)
    token = generate_ack_token(alert_id, notification_id, config=cfg)
    base = cfg.frontend_url.rstrip("/")
    return f"{base}/fall-alerts/ack?token={token}"
