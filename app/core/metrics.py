"""Operational metrics collector for the HealthKicks Cloud API."""

from __future__ import annotations

import threading
import time
from typing import Any


DEFAULT_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
class MetricsCollector:
    """Thread-safe in-memory operational metrics collector."""

    def __init__(self, buckets: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS) -> None:
        self._lock = threading.Lock()
        self._start_time = time.monotonic()
        self._total_requests = 0
        self._total_duration_ms = 0.0
        self._status_2xx = 0
        self._status_3xx = 0
        self._status_4xx = 0
        self._status_5xx = 0
        self._routes: dict[str, int] = {}
        self._buckets = buckets
        self._bucket_counts: dict[float, int] = {b: 0 for b in self._buckets}

    def record_request(self, method: str, path: str, status_code: int, duration_ms: float) -> None:
        """Record telemetry for a single HTTP transaction."""
        key = f"{method} {path}"
        dur_sec = duration_ms / 1000.0
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

            for b in self._buckets:
                if dur_sec <= b:
                    self._bucket_counts[b] += 1

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

    def get_prometheus_text(self, database_pool: dict[str, Any] | None = None) -> str:
        """Render operational metrics in standard Prometheus exposition format."""
        lines: list[str] = [
            "# HELP healthkicks_uptime_seconds Time since application startup in seconds",
            "# TYPE healthkicks_uptime_seconds gauge",
            f"healthkicks_uptime_seconds {self.get_uptime_seconds()}",
            "",
            "# HELP healthkicks_http_requests_total Total number of HTTP requests processed by status class",
            "# TYPE healthkicks_http_requests_total counter",
        ]
        with self._lock:
            lines.append(f'healthkicks_http_requests_total{{status_class="2xx"}} {self._status_2xx}')
            lines.append(f'healthkicks_http_requests_total{{status_class="3xx"}} {self._status_3xx}')
            lines.append(f'healthkicks_http_requests_total{{status_class="4xx"}} {self._status_4xx}')
            lines.append(f'healthkicks_http_requests_total{{status_class="5xx"}} {self._status_5xx}')
            lines.append("")

            lines.extend([
                "# HELP healthkicks_http_request_duration_seconds Latency histogram for HTTP requests",
                "# TYPE healthkicks_http_request_duration_seconds histogram",
            ])
            for b in self._buckets:
                lines.append(f'healthkicks_http_request_duration_seconds_bucket{{le="{b}"}} {self._bucket_counts[b]}')
            lines.append(f'healthkicks_http_request_duration_seconds_bucket{{le="+Inf"}} {self._total_requests}')
            lines.append(f'healthkicks_http_request_duration_seconds_sum {round(self._total_duration_ms / 1000.0, 6)}')
            lines.append(f'healthkicks_http_request_duration_seconds_count {self._total_requests}')
            lines.append("")

            avg_latency = (
                round((self._total_duration_ms / self._total_requests) / 1000.0, 4)
                if self._total_requests > 0
                else 0.0
            )
            lines.extend([
                "# HELP healthkicks_http_request_duration_seconds_average Average HTTP request duration in seconds",
                "# TYPE healthkicks_http_request_duration_seconds_average gauge",
                f"healthkicks_http_request_duration_seconds_average {avg_latency}",
                "",
                "# HELP healthkicks_http_endpoint_requests_total Total requests processed per route",
                "# TYPE healthkicks_http_endpoint_requests_total counter",
            ])
            for route_key, count in sorted(self._routes.items(), key=lambda item: item[1], reverse=True)[:20]:
                parts = route_key.split(" ", 1)
                method = parts[0]
                path = parts[1] if len(parts) > 1 else ""
                lines.append(f'healthkicks_http_endpoint_requests_total{{method="{method}",path="{path}"}} {count}')

        if database_pool:
            lines.extend([
                "",
                "# HELP healthkicks_database_pool_size Connection pool configured size",
                "# TYPE healthkicks_database_pool_size gauge",
                f"healthkicks_database_pool_size {database_pool.get('size', 0)}",
                "",
                "# HELP healthkicks_database_pool_checked_in Idle connections checked in to the pool",
                "# TYPE healthkicks_database_pool_checked_in gauge",
                f"healthkicks_database_pool_checked_in {database_pool.get('checked_in', 0)}",
                "",
                "# HELP healthkicks_database_pool_checked_out Active connections checked out from the pool",
                "# TYPE healthkicks_database_pool_checked_out gauge",
                f"healthkicks_database_pool_checked_out {database_pool.get('checked_out', 0)}",
                "",
                "# HELP healthkicks_database_pool_overflow Overflow connections in the pool",
                "# TYPE healthkicks_database_pool_overflow gauge",
                f"healthkicks_database_pool_overflow {database_pool.get('overflow', 0)}",
            ])

        lines.append("")
        return "\n".join(lines)


# Global singleton instance
metrics_collector = MetricsCollector()

