"""Tests for telemetry storage and anomaly detection."""

import math

from app.models.telemetry_model import IMUTelemetry
from app.services.ai_service import AIService
from app.services.telemetry_service import TelemetryService


def make_telemetry(index: int = 0) -> IMUTelemetry:
    """Create a deterministic normal walking sample."""
    return IMUTelemetry(
        ax=math.sin(index / 5) * 0.2,
        ay=math.cos(index / 5) * 0.2,
        az=1.0 + math.sin(index / 3) * 0.4,
        gx=0.0,
        gy=0.0,
        gz=0.0,
    )


def test_telemetry_service_keeps_bounded_history() -> None:
    service = TelemetryService(max_size=2)
    samples = [make_telemetry(index) for index in range(3)]

    for sample in samples:
        service.record(sample)

    assert service.latest() == samples[-1]
    assert service.history() == samples[-2:]


def test_ai_service_warms_up_before_inference() -> None:
    service = AIService(
        lambda command: True,
        min_training_samples=3,
        training_window=5,
    )

    result = service.process(make_telemetry())

    assert result.model_ready is False
    assert result.is_anomaly is False
    assert result.sample_count == 1
    assert result.anomaly_score is None


def test_ai_service_detects_impact_and_publishes_haptic_command() -> None:
    published_commands = []
    service = AIService(
        lambda command: published_commands.append(command) or True,
        min_training_samples=20,
        training_window=50,
        retrain_interval=1_000,
        haptic_cooldown_seconds=0,
    )

    for index in range(30):
        service.process(make_telemetry(index))

    result = service.process(
        IMUTelemetry(ax=1.5, ay=1.5, az=4.5, gx=80, gy=80, gz=80)
    )

    assert result.model_ready is True
    assert result.is_anomaly is True
    assert result.haptic_triggered is True
    assert len(published_commands) >= 1
    assert published_commands[0].intensity == 220
    assert published_commands[0].duration_ms == 500


def test_ai_service_respects_haptic_cooldown() -> None:
    class AlwaysAnomalyModel:
        def predict(self, samples):
            return [-1]

        def decision_function(self, samples):
            return [-1.0]

    published_commands = []
    service = AIService(
        lambda command: published_commands.append(command) or True,
        min_training_samples=100,
        training_window=30,
        retrain_interval=1_000,
        haptic_cooldown_seconds=60,
    )
    service._model = AlwaysAnomalyModel()

    first = service.process(make_telemetry())
    second = service.process(make_telemetry(1))

    assert first.is_anomaly is True
    assert second.is_anomaly is True
    assert first.haptic_triggered is True
    assert second.haptic_triggered is False
    assert len(published_commands) == 1
