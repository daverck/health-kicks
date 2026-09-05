"""Application configuration loaded from YAML and environment overrides."""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Callable

import yaml
from dotenv import load_dotenv

load_dotenv()

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings for the Cloud API."""

    aws_iot_endpoint: str = ""
    aws_iot_haptic_command_topic: str = "healthkicks/v1/{device_id}/commands/haptic"
    database_url: str = "postgresql+psycopg2://healthkicks:healthkicks@localhost:5432/healthkicks"
    auto_create_tables: bool = True
    aws_region: str = "eu-north-1"
    ingest_token: str = ""
    environment: str = "development"
    # RDS IAM authentication (token regenerated per connection, 15 min expiry)
    use_rds_iam: bool = False
    database_sslmode: str = "require"
    # Google SSO / JWT (Step 1)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:4200/auth/google/callback"
    jwt_secret: str = "dev-only-insecure-secret"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    public_origins: list[str] | None = None
    device_inactivity_days: int = 30
    migrate_on_start: bool = True
    # Microsoft Entra ID (Azure AD) SSO
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_tenant_id: str = "common"
    azure_redirect_uri: str = ""


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML mapping, returning an empty mapping when the file is absent."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as config_file:
        values = yaml.safe_load(config_file) or {}
    if not isinstance(values, dict):
        raise ValueError(f"Configuration root must be a mapping: {path}")
    return values


def _nested_value(values: dict[str, Any], section: str, key: str, default: Any) -> Any:
    """Read one value from a nested YAML section."""
    section_values = values.get(section, {})
    if not isinstance(section_values, dict):
        raise ValueError(f"Configuration section '{section}' must be a mapping")
    return section_values.get(key, default)


def load_settings(config_path: Path | str | None = None) -> Settings:
    """Load YAML settings and apply environment variables as final overrides."""
    path = Path(
        config_path
        or os.getenv("HEALTHKICKS_CONFIG_FILE")
        or DEFAULT_CONFIG_PATH
    ).expanduser().resolve()
    yaml_values = _read_yaml(path)
    defaults = Settings()

    values: dict[str, Any] = {
        "aws_iot_endpoint": _nested_value(
            yaml_values, "aws_iot", "endpoint", defaults.aws_iot_endpoint
        ),
        "aws_iot_haptic_command_topic": _nested_value(
            yaml_values,
            "aws_iot",
            "haptic_command_topic",
            defaults.aws_iot_haptic_command_topic,
        ),
        "database_url": yaml_values.get("database_url", defaults.database_url),
        "auto_create_tables": yaml_values.get("auto_create_tables", defaults.auto_create_tables),
        "aws_region": yaml_values.get("aws_region", defaults.aws_region),
        "ingest_token": yaml_values.get("ingest_token", defaults.ingest_token),
        "environment": yaml_values.get("environment", defaults.environment),
        "use_rds_iam": yaml_values.get("use_rds_iam", defaults.use_rds_iam),
        "database_sslmode": yaml_values.get("database_sslmode", defaults.database_sslmode),
        # Google SSO / JWT (Step 1); support flat keys and a nested [auth] section.
        "google_client_id": _nested_value(yaml_values, "auth", "google_client_id", defaults.google_client_id),
        "google_client_secret": _nested_value(yaml_values, "auth", "google_client_secret", defaults.google_client_secret),
        "google_redirect_uri": _nested_value(yaml_values, "auth", "google_redirect_uri", defaults.google_redirect_uri),
        "jwt_secret": _nested_value(yaml_values, "auth", "jwt_secret", defaults.jwt_secret),
        "jwt_algorithm": _nested_value(yaml_values, "auth", "jwt_algorithm", defaults.jwt_algorithm),
        "access_token_expire_minutes": int(
            _nested_value(yaml_values, "auth", "access_token_expire_minutes", defaults.access_token_expire_minutes)
        ),
        "public_origins": yaml_values.get("public_origins", defaults.public_origins),
        "device_inactivity_days": int(
            yaml_values.get("device_inactivity_days", defaults.device_inactivity_days)
        ),
        "migrate_on_start": bool(
            yaml_values.get("migrate_on_start", defaults.migrate_on_start)
        ),
        # Microsoft Entra ID (Azure AD) SSO
        "azure_client_id": _nested_value(yaml_values, "auth", "azure_client_id", defaults.azure_client_id),
        "azure_client_secret": _nested_value(yaml_values, "auth", "azure_client_secret", defaults.azure_client_secret),
        "azure_tenant_id": _nested_value(yaml_values, "auth", "azure_tenant_id", defaults.azure_tenant_id),
        "azure_redirect_uri": _nested_value(yaml_values, "auth", "azure_redirect_uri", defaults.azure_redirect_uri),
    }

    environment_overrides: dict[str, tuple[tuple[str, ...], Callable[[str], Any]]] = {
        "aws_iot_endpoint": (("HEALTHKICKS_AWS_IOT_ENDPOINT", "AWS_IOT_ENDPOINT"), str),
        "aws_iot_haptic_command_topic": (("HEALTHKICKS_AWS_IOT_HAPTIC_COMMAND_TOPIC", "AWS_IOT_HAPTIC_COMMAND_TOPIC"), str),
        "database_url": (("DATABASE_URL", "HEALTHKICKS_DATABASE_URL"), str),
        "auto_create_tables": (("HEALTHKICKS_AUTO_CREATE_TABLES",), lambda value: value.lower() in {"1", "true", "yes"}),
        "aws_region": (("AWS_REGION", "AWS_DEFAULT_REGION"), str),
        "ingest_token": (("HEALTHKICKS_INGEST_TOKEN",), str),
        "environment": (("HEALTHKICKS_ENVIRONMENT",), str),
        "use_rds_iam": (("USE_RDS_IAM", "HEALTHKICKS_USE_RDS_IAM"), lambda value: value.lower() in {"1", "true", "yes"}),
        "database_sslmode": (("DATABASE_SSLMODE", "HEALTHKICKS_DATABASE_SSLMODE"), str),
        # Google SSO / JWT (Step 1)
        "google_client_id": (("HEALTHKICKS_GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_ID"), str),
        "google_client_secret": (("HEALTHKICKS_GOOGLE_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET"), str),
        "google_redirect_uri": (
            (
                "HEALTHKICKS_GOOGLE_REDIRECT_URI",
                "GOOGLE_REDIRECT_URI",
            ),
            str,
        ),
        "jwt_secret": (("HEALTHKICKS_JWT_SECRET", "JWT_SECRET"), str),
        "jwt_algorithm": (("HEALTHKICKS_JWT_ALGORITHM",), str),
        "access_token_expire_minutes": (("HEALTHKICKS_ACCESS_TOKEN_EXPIRE_MINUTES",), int),
        "public_origins": (("HEALTHKICKS_PUBLIC_ORIGINS",), lambda value: [origin.strip() for origin in value.split(",") if origin.strip()]),
        "device_inactivity_days": (("HEALTHKICKS_DEVICE_INACTIVITY_DAYS", "DEVICE_INACTIVITY_DAYS"), int),
        "migrate_on_start": (("MIGRATE_ON_START", "HEALTHKICKS_MIGRATE_ON_START"), lambda value: value.lower() in {"1", "true", "yes"}),
        # Microsoft Entra ID (Azure AD) SSO
        "azure_client_id": (("HEALTHKICKS_AZURE_CLIENT_ID", "AZURE_CLIENT_ID"), str),
        "azure_client_secret": (("HEALTHKICKS_AZURE_CLIENT_SECRET", "AZURE_CLIENT_SECRET"), str),
        "azure_tenant_id": (("HEALTHKICKS_AZURE_TENANT_ID", "AZURE_TENANT_ID"), str),
        "azure_redirect_uri": (("HEALTHKICKS_AZURE_REDIRECT_URI", "AZURE_REDIRECT_URI"), str),
    }
    for field_name, (environment_names, converter) in environment_overrides.items():
        for environment_name in environment_names:
            if environment_value := os.getenv(environment_name):
                values[field_name] = converter(environment_value)
                break

    return Settings(**values)


settings = load_settings()
