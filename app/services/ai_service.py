"""Isolation Forest anomaly detection for IMU telemetry."""

from collections import deque
from threading import Lock
import time
from typing import Callable

from sklearn.ensemble import IsolationForest

from app.models.haptic_model import HapticCommand
from app.models.telemetry_model import AIInferenceResult, IMUTelemetry

HapticPublisher = Callable[[HapticCommand], bool]


class AIService:
    """Train and run an Isolation Forest over a sliding IMU feature window."""

    def __init__(
        self,
        haptic_publisher: HapticPublisher,
        training_window: int = 100,
        min_training_samples: int = 30,
        retrain_interval: int = 25,
        contamination: float = 0.05,
        haptic_cooldown_seconds: float = 2.0,
    ) -> None:
        self._samples: deque[list[float]] = deque(maxlen=training_window)
        self._model: IsolationForest | None = None
        self._model_lock = Lock()
        self._haptic_publisher = haptic_publisher
        self._min_training_samples = min_training_samples
        self._retrain_interval = max(1, retrain_interval)
        self._samples_since_fit = 0
        self._contamination = contamination
        self._haptic_cooldown_seconds = haptic_cooldown_seconds
        self._last_haptic_at = 0.0

    def process(self, telemetry: IMUTelemetry) -> AIInferenceResult:
        """Add a sample, retrain when needed, infer, and optionally alert haptically."""
        self._samples.append(telemetry.features())
        sample_count = len(self._samples)

        if self._model is None and sample_count >= self._min_training_samples:
            self._fit_model()
        elif self._model is not None:
            self._samples_since_fit += 1
            if self._samples_since_fit >= self._retrain_interval:
                self._fit_model()

        with self._model_lock:
            model = self._model
            if model is None:
                return AIInferenceResult(
                    is_anomaly=False,
                    model_ready=False,
                    sample_count=sample_count,
                )
            prediction = int(model.predict([telemetry.features()])[0])
            decision_score = float(model.decision_function([telemetry.features()])[0])

        is_anomaly = prediction == -1
        haptic_triggered = is_anomaly and self._trigger_haptic_if_ready()
        return AIInferenceResult(
            is_anomaly=is_anomaly,
            anomaly_score=round(-decision_score, 6),
            model_ready=True,
            sample_count=sample_count,
            haptic_triggered=haptic_triggered,
        )

    def update_configuration(self, anomaly_contamination: float | None = None) -> None:
        """Apply Shadow configuration and retrain with the next telemetry sample."""
        if anomaly_contamination is None:
            return
        if not 0 < anomaly_contamination < 0.5:
            raise ValueError("anomaly_contamination must be between 0 and 0.5")
        self._contamination = anomaly_contamination
        with self._model_lock:
            self._model = None
        self._samples_since_fit = 0

    def _fit_model(self) -> None:
        model = IsolationForest(
            n_estimators=100,
            contamination=self._contamination,
            random_state=42,
        )
        model.fit(list(self._samples))
        with self._model_lock:
            self._model = model
        self._samples_since_fit = 0

    def _trigger_haptic_if_ready(self) -> bool:
        now = time.monotonic()
        if now - self._last_haptic_at < self._haptic_cooldown_seconds:
            return False
        self._last_haptic_at = now
        return self._haptic_publisher(HapticCommand(intensity=220, duration_ms=500))
