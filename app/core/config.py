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
    }

    environment_overrides: dict[str, tuple[tuple[str, ...], Callable[[str], Any]]] = {
        "aws_iot_endpoint": (("HEALTHKICKS_AWS_IOT_ENDPOINT", "AWS_IOT_ENDPOINT"), str),
        "aws_iot_haptic_command_topic": (("HEALTHKICKS_AWS_IOT_HAPTIC_COMMAND_TOPIC", "AWS_IOT_HAPTIC_COMMAND_TOPIC"), str),
        "database_url": (("DATABASE_URL", "HEALTHKICKS_DATABASE_URL"), str),
        "auto_create_tables": (("HEALTHKICKS_AUTO_CREATE_TABLES",), lambda value: value.lower() in {"1", "true", "yes"}),
        "aws_region": (("AWS_REGION", "AWS_DEFAULT_REGION"), str),
        "ingest_token": (("HEALTHKICKS_INGEST_TOKEN",), str),
        "environment": (("HEALTHKICKS_ENVIRONMENT",), str),
    }
    for field_name, (environment_names, converter) in environment_overrides.items():
        for environment_name in environment_names:
            if environment_value := os.getenv(environment_name):
                values[field_name] = converter(environment_value)
                break

    return Settings(**values)


settings = load_settings()
