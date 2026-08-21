"""Telemetry and anomaly inference schemas."""

from time import time

from pydantic import BaseModel, Field


class IMUTelemetry(BaseModel):
    """One IMU sample received from the connected shoe."""

    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float
    timestamp: float = Field(default_factory=time)

    def features(self) -> list[float]:
        """Return the six numeric features expected by the ML model."""
        return [self.ax, self.ay, self.az, self.gx, self.gy, self.gz]


class AIInferenceResult(BaseModel):
    """Result of the anomaly detector for one telemetry sample."""

    is_anomaly: bool
    anomaly_score: float | None = None
    model_ready: bool
    sample_count: int
    haptic_triggered: bool = False
