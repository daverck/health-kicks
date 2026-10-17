"""Operational metrics collector for the HealthKicks Cloud API."""

from __future__ import annotations

import threading
import time
from typing import Any


class MetricsCollector:
    """Thread-safe in-memory operational metrics collector."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start_time = time.monotonic()
        self._total_requests = 0
        self._total_duration_ms = 0.0
        self._status_2xx = 0
        self._status_3xx = 0
        self._status_4xx = 0
        self._status_5xx = 0
        self._routes: dict[str, int] = {}

    def record_request(self, method: str, path: str, status_code: int, duration_ms: float) -> None:
        """Record telemetry for a single HTTP transaction."""
        key = f"{method} {path}"
        with self._lock:
            self._total_requests += 1
            self._total_duration_ms += duration_ms

            if 200 <= status_code < 300:
                self._status_2xx += 1
            elif 300 <= status_code < 400:
                self._status_3xx += 1
            elif 400 <= status_code < 500:
                self._status_4xx += 1
            elif status_code >= 500:
                self._status_5xx += 1

            self._routes[key] = self._routes.get(key, 0) + 1

    def get_uptime_seconds(self) -> float:
        """Return the uptime in seconds since process initialization."""
        return round(time.monotonic() - self._start_time, 2)

    def get_summary(self) -> dict[str, Any]:
        """Return a structured summary of operational metrics."""
        with self._lock:
            avg_latency = (
                round(self._total_duration_ms / self._total_requests, 2)
                if self._total_requests > 0
                else 0.0
            )
            return {
                "uptime_seconds": self.get_uptime_seconds(),
                "total_requests": self._total_requests,
                "average_latency_ms": avg_latency,
                "status_distribution": {
                    "2xx": self._status_2xx,
                    "3xx": self._status_3xx,
                    "4xx": self._status_4xx,
                    "5xx": self._status_5xx,
                },
                "top_endpoints": dict(
                    sorted(self._routes.items(), key=lambda item: item[1], reverse=True)[:10]
                ),
            }


# Global singleton instance
metrics_collector = MetricsCollector()

