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
    assert settings.azure_client_id == ""
    assert settings.azure_client_secret == ""
    assert settings.azure_tenant_id == "common"
    assert settings.azure_redirect_uri == ""
    assert settings.google_redirect_uri == "http://localhost:4200/auth/google/callback"
    assert settings.refresh_token_expire_days == 7
    assert settings.dynamodb_telemetry_table == "healthkicks_telemetry"
    assert settings.aws_iot_studio_start_topic == "healthkicks/v1/{device_id}/commands/studio/start"
    assert settings.database_pool_size == 5
    assert settings.database_max_overflow == 10
    assert settings.database_pool_recycle == 300
    assert settings.database_pool_pre_ping is True
    assert settings.log_level == "INFO"


def test_azure_environment_overrides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("AZURE_TENANT_ID", "test-tenant-id")
    monkeypatch.setenv("AZURE_REDIRECT_URI", "https://healthkicks.duckdns.org/auth/azure/callback")

    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.azure_client_id == "test-client-id"
    assert settings.azure_client_secret == "test-client-secret"
    assert settings.azure_tenant_id == "test-tenant-id"
    assert settings.azure_redirect_uri == "https://healthkicks.duckdns.org/auth/azure/callback"


def test_google_environment_overrides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://healthkicks.duckdns.org/auth/google/callback")
    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.google_redirect_uri == "https://healthkicks.duckdns.org/auth/google/callback"


def test_refresh_token_expire_days_environment_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REFRESH_TOKEN_EXPIRE_DAYS", "14")
    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.refresh_token_expire_days == 14


def test_dynamodb_telemetry_table_environment_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DYNAMODB_TELEMETRY_TABLE", "custom_telemetry_table")
    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.dynamodb_telemetry_table == "custom_telemetry_table"


def test_aws_iot_studio_start_topic_environment_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AWS_IOT_STUDIO_START_TOPIC", "custom/studio/{device_id}/start")
    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.aws_iot_studio_start_topic == "custom/studio/{device_id}/start"


def test_database_pool_and_log_environment_overrides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_POOL_SIZE", "15")
    monkeypatch.setenv("DATABASE_MAX_OVERFLOW", "25")
    monkeypatch.setenv("DATABASE_POOL_RECYCLE", "600")
    monkeypatch.setenv("DATABASE_POOL_PRE_PING", "false")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.database_pool_size == 15
    assert settings.database_max_overflow == 25
    assert settings.database_pool_recycle == 600
    assert settings.database_pool_pre_ping is False
    assert settings.log_level == "DEBUG"
