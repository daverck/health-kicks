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
    """Runtime settings for the API and MQTT integration."""

    mqtt_broker: str = "localhost"
    mqtt_port: int = 1883
    mqtt_keepalive: int = 60
    mqtt_telemetry_topic: str = "chaussure/imu/telemetry"
    mqtt_haptic_topic: str = "chaussure/haptic/cmd"
    mqtt_client_id: str = "FastAPI_Backend"
    telemetry_history_size: int = 100
    ai_training_window: int = 100
    ai_min_training_samples: int = 30
    ai_retrain_interval: int = 25
    ai_contamination: float = 0.05
    ai_haptic_cooldown_seconds: float = 2.0
    aws_iot_endpoint: str = ""
    aws_iot_connect_timeout_seconds: float = 15.0
    aws_iot_thing_name: str = "healthkicks"
    aws_iot_client_id: str = "healthkicks-backend"
    aws_iot_cert_path: str = "certs/certificate.pem.crt"
    aws_iot_private_key_path: str = "certs/private.pem.key"
    aws_iot_root_ca_path: str = "certs/AmazonRootCA1.pem"
    aws_iot_telemetry_topic: str = "healthkicks/devices/healthkicks/telemetry/imu"
    aws_iot_haptic_command_topic: str = "healthkicks/v1/{device_id}/commands/haptic"
    aws_iot_shadow_update_topic: str = "$aws/things/healthkicks/shadow/update"
    aws_iot_shadow_get_topic: str = "$aws/things/healthkicks/shadow/get"
    database_url: str = "sqlite:///./healthkicks.db"
    auto_create_tables: bool = True
    aws_region: str = "eu-north-1"


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


def _resolve_path(value: str, base_dir: Path) -> str:
    """Resolve a relative configuration path without changing absolute paths."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return str(path.resolve())


def load_settings(config_path: Path | str | None = None) -> Settings:
    """Load YAML settings and apply environment variables as final overrides."""
    path = Path(
        config_path
        or os.getenv("HEALTHKICKS_CONFIG_FILE")
        or os.getenv("SMARTSTRIDE_CONFIG_FILE")
        or DEFAULT_CONFIG_PATH
    ).expanduser().resolve()
    yaml_values = _read_yaml(path)
    defaults = Settings()

    values: dict[str, Any] = {
        "mqtt_broker": _nested_value(yaml_values, "mqtt", "broker", defaults.mqtt_broker),
        "mqtt_port": _nested_value(yaml_values, "mqtt", "port", defaults.mqtt_port),
        "mqtt_keepalive": _nested_value(
            yaml_values, "mqtt", "keepalive", defaults.mqtt_keepalive
        ),
        "mqtt_telemetry_topic": _nested_value(
            yaml_values, "mqtt", "telemetry_topic", defaults.mqtt_telemetry_topic
        ),
        "mqtt_haptic_topic": _nested_value(
            yaml_values, "mqtt", "haptic_topic", defaults.mqtt_haptic_topic
        ),
        "mqtt_client_id": _nested_value(
            yaml_values, "mqtt", "client_id", defaults.mqtt_client_id
        ),
        "telemetry_history_size": _nested_value(
            yaml_values, "telemetry", "history_size", defaults.telemetry_history_size
        ),
        "ai_training_window": _nested_value(
            yaml_values, "ai", "training_window", defaults.ai_training_window
        ),
        "ai_min_training_samples": _nested_value(
            yaml_values, "ai", "min_training_samples", defaults.ai_min_training_samples
        ),
        "ai_retrain_interval": _nested_value(
            yaml_values, "ai", "retrain_interval", defaults.ai_retrain_interval
        ),
        "ai_contamination": _nested_value(
            yaml_values, "ai", "contamination", defaults.ai_contamination
        ),
        "ai_haptic_cooldown_seconds": _nested_value(
            yaml_values,
            "ai",
            "haptic_cooldown_seconds",
            defaults.ai_haptic_cooldown_seconds,
        ),
        "aws_iot_endpoint": _nested_value(
            yaml_values, "aws_iot", "endpoint", defaults.aws_iot_endpoint
        ),
        "aws_iot_connect_timeout_seconds": _nested_value(
            yaml_values,
            "aws_iot",
            "connect_timeout_seconds",
            defaults.aws_iot_connect_timeout_seconds,
        ),
        "aws_iot_thing_name": _nested_value(
            yaml_values, "aws_iot", "thing_name", defaults.aws_iot_thing_name
        ),
        "aws_iot_client_id": _nested_value(
            yaml_values, "aws_iot", "client_id", defaults.aws_iot_client_id
        ),
        "aws_iot_cert_path": _nested_value(
            yaml_values, "aws_iot", "cert_path", defaults.aws_iot_cert_path
        ),
        "aws_iot_private_key_path": _nested_value(
            yaml_values, "aws_iot", "private_key_path", defaults.aws_iot_private_key_path
        ),
        "aws_iot_root_ca_path": _nested_value(
            yaml_values, "aws_iot", "root_ca_path", defaults.aws_iot_root_ca_path
        ),
        "aws_iot_telemetry_topic": _nested_value(
            yaml_values, "aws_iot", "telemetry_topic", defaults.aws_iot_telemetry_topic
        ),
        "aws_iot_haptic_command_topic": _nested_value(
            yaml_values,
            "aws_iot",
            "haptic_command_topic",
            defaults.aws_iot_haptic_command_topic,
        ),
        "aws_iot_shadow_update_topic": _nested_value(
            yaml_values,
            "aws_iot",
            "shadow_update_topic",
            defaults.aws_iot_shadow_update_topic,
        ),
        "aws_iot_shadow_get_topic": _nested_value(
            yaml_values, "aws_iot", "shadow_get_topic", defaults.aws_iot_shadow_get_topic
        ),
        "database_url": yaml_values.get("database_url", defaults.database_url),
            "auto_create_tables": yaml_values.get("auto_create_tables", defaults.auto_create_tables),
        "aws_region": yaml_values.get("aws_region", defaults.aws_region),
    }

    environment_overrides: dict[str, tuple[tuple[str, ...], Callable[[str], Any]]] = {
        "mqtt_broker": (("HEALTHKICKS_MQTT_BROKER", "SMARTSTRIDE_MQTT_BROKER"), str),
        "mqtt_port": (("HEALTHKICKS_MQTT_PORT", "SMARTSTRIDE_MQTT_PORT"), int),
        "mqtt_keepalive": (("HEALTHKICKS_MQTT_KEEPALIVE", "SMARTSTRIDE_MQTT_KEEPALIVE"), int),
        "mqtt_telemetry_topic": (("HEALTHKICKS_MQTT_TELEMETRY_TOPIC", "SMARTSTRIDE_MQTT_TELEMETRY_TOPIC"), str),
        "mqtt_haptic_topic": (("HEALTHKICKS_MQTT_HAPTIC_TOPIC", "SMARTSTRIDE_MQTT_HAPTIC_TOPIC"), str),
        "mqtt_client_id": (("HEALTHKICKS_MQTT_CLIENT_ID", "SMARTSTRIDE_MQTT_CLIENT_ID"), str),
        "telemetry_history_size": (("HEALTHKICKS_TELEMETRY_HISTORY_SIZE", "SMARTSTRIDE_TELEMETRY_HISTORY_SIZE"), int),
        "ai_training_window": (("HEALTHKICKS_AI_TRAINING_WINDOW", "SMARTSTRIDE_AI_TRAINING_WINDOW"), int),
        "ai_min_training_samples": (("HEALTHKICKS_AI_MIN_TRAINING_SAMPLES", "SMARTSTRIDE_AI_MIN_TRAINING_SAMPLES"), int),
        "ai_retrain_interval": (("HEALTHKICKS_AI_RETRAIN_INTERVAL", "SMARTSTRIDE_AI_RETRAIN_INTERVAL"), int),
        "ai_contamination": (("HEALTHKICKS_AI_CONTAMINATION", "SMARTSTRIDE_AI_CONTAMINATION"), float),
        "ai_haptic_cooldown_seconds": ((
            "HEALTHKICKS_AI_HAPTIC_COOLDOWN_SECONDS",
            "SMARTSTRIDE_AI_HAPTIC_COOLDOWN_SECONDS",
        ),
            float,
        ),
        "aws_iot_endpoint": (("HEALTHKICKS_AWS_IOT_ENDPOINT", "AWS_IOT_ENDPOINT"), str),
        "aws_iot_connect_timeout_seconds": (
            ("HEALTHKICKS_AWS_IOT_CONNECT_TIMEOUT_SECONDS", "AWS_IOT_CONNECT_TIMEOUT_SECONDS"),
            float,
        ),
        "aws_iot_thing_name": (("HEALTHKICKS_AWS_IOT_THING_NAME", "AWS_IOT_THING_NAME"), str),
        "aws_iot_client_id": (("HEALTHKICKS_AWS_IOT_CLIENT_ID", "AWS_IOT_CLIENT_ID"), str),
        "aws_iot_cert_path": (("HEALTHKICKS_AWS_IOT_CERT_PATH", "AWS_IOT_CERT_PATH"), str),
        "aws_iot_private_key_path": (("HEALTHKICKS_AWS_IOT_PRIVATE_KEY_PATH", "AWS_IOT_PRIVATE_KEY_PATH"), str),
        "aws_iot_root_ca_path": (("HEALTHKICKS_AWS_IOT_ROOT_CA_PATH", "AWS_IOT_ROOT_CA_PATH"), str),
        "aws_iot_telemetry_topic": (("HEALTHKICKS_AWS_IOT_TELEMETRY_TOPIC", "AWS_IOT_TELEMETRY_TOPIC"), str),
        "aws_iot_haptic_command_topic": (("HEALTHKICKS_AWS_IOT_HAPTIC_COMMAND_TOPIC", "AWS_IOT_HAPTIC_COMMAND_TOPIC"), str),
        "aws_iot_shadow_update_topic": (("HEALTHKICKS_AWS_IOT_SHADOW_UPDATE_TOPIC", "AWS_IOT_SHADOW_UPDATE_TOPIC"), str),
        "aws_iot_shadow_get_topic": (("HEALTHKICKS_AWS_IOT_SHADOW_GET_TOPIC", "AWS_IOT_SHADOW_GET_TOPIC"), str),
        "database_url": (("DATABASE_URL", "HEALTHKICKS_DATABASE_URL"), str),
            "auto_create_tables": (("HEALTHKICKS_AUTO_CREATE_TABLES",), lambda value: value.lower() in {"1", "true", "yes"}),
        "aws_region": (("AWS_REGION", "AWS_DEFAULT_REGION"), str),
    }
    for field_name, (environment_names, converter) in environment_overrides.items():
        for environment_name in environment_names:
            if environment_value := os.getenv(environment_name):
                values[field_name] = converter(environment_value)
                break

    config_directory = path.parent
    for field_name in (
        "aws_iot_cert_path",
        "aws_iot_private_key_path",
        "aws_iot_root_ca_path",
    ):
        values[field_name] = _resolve_path(values[field_name], config_directory)

    return Settings(**values)


settings = load_settings()
