"""Unit tests for sliding-window rate limiting on sensitive API endpoints."""

import time
from fastapi import APIRouter, Depends, FastAPI, status
from fastapi.testclient import TestClient

from app.core.rate_limit import SlidingWindowRateLimiter, rate_limit, rate_limiter


def test_sliding_window_unit_logic() -> None:
    limiter = SlidingWindowRateLimiter()
    key = "test_key"

    # Allow 3 requests in a 2-second window
    assert limiter.is_allowed(key, max_requests=3, window_seconds=2.0)[0] is True
    assert limiter.is_allowed(key, max_requests=3, window_seconds=2.0)[0] is True
    assert limiter.is_allowed(key, max_requests=3, window_seconds=2.0)[0] is True

    # 4th request must be rejected
    allowed, retry_after = limiter.is_allowed(key, max_requests=3, window_seconds=2.0)
    assert allowed is False
    assert retry_after >= 1

    # Reset clears history
    limiter.reset()
    assert limiter.is_allowed(key, max_requests=3, window_seconds=2.0)[0] is True


def test_fastapi_rate_limit_dependency_blocks_excess_requests() -> None:
    rate_limiter.reset()
    app = FastAPI()
    router = APIRouter()

    @router.post(
        "/sensitive-action",
        dependencies=[Depends(rate_limit("test_action", max_requests=2, window_seconds=60.0))],
    )
    def sensitive_action():
        return {"ok": True}

    app.include_router(router)
    client = TestClient(app)

    # 1st and 2nd requests succeed
    res1 = client.post("/sensitive-action")
    assert res1.status_code == 200

    res2 = client.post("/sensitive-action")
    assert res2.status_code == 200

    # 3rd request receives 429 Too Many Requests
    res3 = client.post("/sensitive-action")
    assert res3.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert "Too many requests for test_action" in res3.json()["detail"]
    assert "Retry-After" in res3.headers

    rate_limiter.reset()

