"""Tests for Cloud configuration defaults and environment overrides."""

from pathlib import Path

from app.core.config import load_settings


def test_database_defaults_to_postgresql(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "missing.yaml").database_url.startswith("postgresql+psycopg2://")


def test_environment_overrides_database_and_ingest_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("HEALTHKICKS_INGEST_TOKEN", "secret")
    monkeypatch.setenv("MIGRATE_ON_START", "false")
    monkeypatch.setenv("HEALTHKICKS_DEVICE_INACTIVITY_DAYS", "45")
    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.database_url == "sqlite:///:memory:"
    assert settings.ingest_token == "secret"
    assert settings.migrate_on_start is False
    assert settings.device_inactivity_days == 45


def test_settings_defaults(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.migrate_on_start is True
    assert settings.device_inactivity_days == 30
    assert settings.AZURE_CLIENT_ID == ""
    assert settings.AZURE_CLIENT_SECRET == ""
    assert settings.AZURE_TENANT_ID == "common"
    assert settings.AZURE_REDIRECT_URI == ""
    assert settings.azure_tenant_id == "common"


def test_azure_environment_overrides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("AZURE_TENANT_ID", "test-tenant-id")
    monkeypatch.setenv("AZURE_REDIRECT_URI", "https://healthkicks.duckdns.org:8443/login/callback")

    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.AZURE_CLIENT_ID == "test-client-id"
    assert settings.azure_client_id == "test-client-id"
    assert settings.AZURE_CLIENT_SECRET == "test-client-secret"
    assert settings.azure_client_secret == "test-client-secret"
    assert settings.AZURE_TENANT_ID == "test-tenant-id"
    assert settings.azure_tenant_id == "test-tenant-id"
    assert settings.AZURE_REDIRECT_URI == "https://healthkicks.duckdns.org:8443/login/callback"
    assert settings.azure_redirect_uri == "https://healthkicks.duckdns.org:8443/login/callback"
