"""Tests for YAML configuration and environment overrides."""

from pathlib import Path

from app.core.config import load_settings


CONFIG_CONTENT = """
mqtt:
  broker: test-broker
  port: 2883
  keepalive: 45
  client_id: test-client
telemetry:
  history_size: 12
ai:
  training_window: 40
  min_training_samples: 15
  retrain_interval: 8
  contamination: 0.1
  haptic_cooldown_seconds: 3.5
"""


def write_config(path: Path) -> Path:
    """Write a temporary YAML configuration for a test."""
    path.write_text(CONFIG_CONTENT, encoding="utf-8")
    return path


def test_load_settings_reads_yaml_values(tmp_path: Path) -> None:
    settings = load_settings(write_config(tmp_path / "config.yaml"))

    assert settings.mqtt_broker == "test-broker"
    assert settings.mqtt_port == 2883
    assert settings.mqtt_keepalive == 45
    assert settings.mqtt_client_id == "test-client"
    assert settings.telemetry_history_size == 12
    assert settings.ai_training_window == 40
    assert settings.ai_min_training_samples == 15
    assert settings.ai_retrain_interval == 8
    assert settings.ai_contamination == 0.1
    assert settings.ai_haptic_cooldown_seconds == 3.5


def test_environment_overrides_yaml_values(tmp_path: Path, monkeypatch) -> None:
  config_path = write_config(tmp_path / "config.yaml")
  monkeypatch.setenv("HEALTHKICKS_MQTT_BROKER", "environment-broker")
  monkeypatch.setenv("HEALTHKICKS_MQTT_PORT", "3883")
  monkeypatch.setenv("HEALTHKICKS_AI_CONTAMINATION", "0.2")

  settings = load_settings(config_path)

  assert settings.mqtt_broker == "environment-broker"
  assert settings.mqtt_port == 3883
  assert settings.ai_contamination == 0.2
  assert settings.mqtt_keepalive == 45
  assert settings.ai_training_window == 40


def test_smartstride_environment_alias_remains_supported(
  tmp_path: Path, monkeypatch
) -> None:
  monkeypatch.setenv("SMARTSTRIDE_MQTT_BROKER", "legacy-broker")

  settings = load_settings(write_config(tmp_path / "config.yaml"))

  assert settings.mqtt_broker == "legacy-broker"


def test_aws_certificate_paths_are_relative_to_config_file(tmp_path: Path) -> None:
  config_path = tmp_path / "config.yaml"
  config_path.write_text(
    "aws_iot:\n"
    "  cert_path: certs/device.crt\n"
    "  private_key_path: certs/device.key\n"
    "  root_ca_path: certs/root-ca.crt\n",
    encoding="utf-8",
  )

  settings = load_settings(config_path)

  assert settings.aws_iot_cert_path == str(
    (tmp_path / "certs/device.crt").resolve()
  )
  assert settings.aws_iot_private_key_path == str(
    (tmp_path / "certs/device.key").resolve()
  )
  assert settings.aws_iot_root_ca_path == str(
    (tmp_path / "certs/root-ca.crt").resolve()
  )
