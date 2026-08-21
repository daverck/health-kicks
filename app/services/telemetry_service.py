"""In-memory telemetry storage."""

from collections import deque
from threading import Lock

from app.models.telemetry_model import IMUTelemetry


class TelemetryService:
    """Store the latest sample and a bounded history in a thread-safe buffer."""

    def __init__(self, max_size: int = 100) -> None:
        self._history: deque[IMUTelemetry] = deque(maxlen=max_size)
        self._latest: IMUTelemetry | None = None
        self._lock = Lock()

    def record(self, telemetry: IMUTelemetry) -> None:
        """Record one telemetry sample."""
        with self._lock:
            self._latest = telemetry
            self._history.append(telemetry)

    def latest(self) -> IMUTelemetry | None:
        """Return the most recent sample, if one exists."""
        with self._lock:
            return self._latest

    def history(self) -> list[IMUTelemetry]:
        """Return a snapshot of the bounded history."""
        with self._lock:
            return list(self._history)
