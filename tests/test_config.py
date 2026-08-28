"""Tests for Cloud configuration defaults and environment overrides."""

from pathlib import Path

from app.core.config import load_settings


def test_database_defaults_to_postgresql(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "missing.yaml").database_url.startswith("postgresql+psycopg2://")


def test_environment_overrides_database_and_ingest_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("HEALTHKICKS_INGEST_TOKEN", "secret")
    settings = load_settings(tmp_path / "missing.yaml")
    assert settings.database_url == "sqlite:///:memory:"
    assert settings.ingest_token == "secret"
