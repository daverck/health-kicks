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


def test_sentry_before_send_filters_db_timeout():
    """Verify _sentry_before_send drops database connection timeouts."""
    from app.main import _sentry_before_send

    # Simulated psycopg2 / SQLAlchemy timeout error
    err = RuntimeError(
        'connection to server at "healthkicks-db.cluster-cb46qko685z2.eu-north-1.rds.amazonaws.com" port 5432 failed: timeout expired'
    )
    hint = {"exc_info": (type(err), err, None)}
    event = {"message": "Database error"}

    assert _sentry_before_send(event, hint) is None


def test_sentry_before_send_preserves_application_errors():
    """Verify _sentry_before_send keeps real application errors."""
    from app.main import _sentry_before_send

    err = ValueError("Invalid user payload or token signature")
    hint = {"exc_info": (type(err), err, None)}
    event = {"message": "Application error"}

    result = _sentry_before_send(event, hint)
    assert result is not None
    assert result["message"] == "Application error"
