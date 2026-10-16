"""In-memory thread-safe sliding-window rate limiter for sensitive endpoints."""

import threading
import time
from collections import defaultdict

from fastapi import HTTPException, Request, status


class SlidingWindowRateLimiter:
    """Thread-safe sliding-window in-memory rate limiter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, key: str, max_requests: int, window_seconds: float) -> tuple[bool, int]:
        """Check if request is allowed under max_requests within window_seconds.

        Returns (allowed, retry_after_seconds).
        """
        now = time.monotonic()
        cutoff = now - window_seconds

        with self._lock:
            timestamps = self._records[key]
            # Prune old timestamps
            valid = [t for t in timestamps if t > cutoff]
            current_count = len(valid)

            if current_count >= max_requests:
                earliest = valid[0]
                retry_after = max(1, int(window_seconds - (now - earliest)))
                self._records[key] = valid
                return False, retry_after

            valid.append(now)
            self._records[key] = valid
            return True, 0

    def reset(self) -> None:
        """Clear all rate limit records (useful for test isolation)."""
        with self._lock:
            self._records.clear()


rate_limiter = SlidingWindowRateLimiter()


def rate_limit(name: str, max_requests: int, window_seconds: float = 60.0):
    """FastAPI dependency factory enforcing rate limits on endpoints."""

    def dependency(request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()

        key = f"{name}:{client_ip}"
        allowed, retry_after = rate_limiter.is_allowed(key, max_requests, window_seconds)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many requests for {name}. Please retry after {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )

    return dependency

