"""Unit tests for SmtpEmailChannel local Mailpit compatibility and production STARTTLS."""

from email.message import EmailMessage
from unittest.mock import MagicMock

import pytest

from app.core.config import Settings
from app.services.email_service import SmtpEmailChannel


def _build_message() -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Test Alert"
    msg["From"] = "alertes@healthkicks.eu"
    msg["To"] = "contact@example.com"
    msg.set_content("Test body")
    return msg


def test_smtp_skips_starttls_in_dev_when_server_lacks_extension() -> None:
    """Local dev SMTP servers such as Mailpit without STARTTLS work without failing."""
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    mock_smtp.has_extn.return_value = False

    cfg = Settings(environment="development", smtp_host="mailpit", smtp_port=1025)
    channel = SmtpEmailChannel(config=cfg, smtp_factory=lambda _h, _p, _t: mock_smtp)
    channel.send_message(_build_message())

    mock_smtp.ehlo.assert_called_once()
    mock_smtp.starttls.assert_not_called()
    mock_smtp.send_message.assert_called_once()


def test_smtp_enforces_starttls_in_production() -> None:
    """Production environment always enforces STARTTLS even if has_extn is false."""
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    mock_smtp.has_extn.return_value = False

    cfg = Settings(environment="production", smtp_host="smtp.gmail.com", smtp_port=587)
    channel = SmtpEmailChannel(config=cfg, smtp_factory=lambda _h, _p, _t: mock_smtp)
    channel.send_message(_build_message())

    mock_smtp.starttls.assert_called_once()
    mock_smtp.send_message.assert_called_once()


@pytest.mark.parametrize("env", ["development", "production"])
def test_smtp_uses_starttls_when_advertised(env: str) -> None:
    """When STARTTLS is advertised by the SMTP server, it is always used."""
    mock_smtp = MagicMock()
    mock_smtp.__enter__.return_value = mock_smtp
    mock_smtp.has_extn.return_value = True

    cfg = Settings(environment=env, smtp_host="smtp.example.com", smtp_port=587)
    channel = SmtpEmailChannel(config=cfg, smtp_factory=lambda _h, _p, _t: mock_smtp)
    channel.send_message(_build_message())

    mock_smtp.starttls.assert_called_once()
    assert mock_smtp.ehlo.call_count == 2
