"""Tests for Sentry configuration and initialization."""

from app.core.config import Settings, load_settings


def test_sentry_settings_default():
    """Verify Sentry settings default to empty DSN."""
    settings = Settings()
    assert settings.sentry_dsn == ""
    assert settings.sentry_traces_sample_rate == 0.2


def test_sentry_settings_env_override(monkeypatch):
    """Verify SENTRY_DSN environment variable correctly populates settings."""
    test_dsn = "https://examplePublicKey@o0.ingest.sentry.io/0"
    monkeypatch.setenv("SENTRY_DSN", test_dsn)
    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.5")

    settings = load_settings()
    assert settings.sentry_dsn == test_dsn
    assert settings.sentry_traces_sample_rate == 0.5
