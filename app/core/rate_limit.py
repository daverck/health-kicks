"""Distributed rate limiter backed by Amazon DynamoDB atomic counters with TTL and in-memory fallback."""

import logging
import threading
import time
from collections import defaultdict
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, Request, status

from app.core.config import Settings, settings

logger = logging.getLogger(__name__)


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


class DynamoDbRateLimiter:
    """Multi-worker distributed rate limiter backed by Amazon DynamoDB atomic counters with TTL."""

    def __init__(
        self,
        table: Any | None = None,
        config: Settings = settings,
        fallback_limiter: SlidingWindowRateLimiter | None = None,
    ) -> None:
        self._table = table
        self._config = config
        self._fallback = fallback_limiter or SlidingWindowRateLimiter()
        self._dynamo_unavailable = False

    def _get_table(self) -> Any | None:
        if self._dynamo_unavailable:
            return None
        if self._table is not None:
            return self._table
        try:
            kwargs: dict[str, Any] = {"region_name": self._config.aws_region}
            if getattr(self._config, "dynamodb_endpoint_url", None):
                kwargs["endpoint_url"] = self._config.dynamodb_endpoint_url
            if getattr(self._config, "aws_access_key_id", None) and getattr(self._config, "aws_secret_access_key", None):
                kwargs["aws_access_key_id"] = self._config.aws_access_key_id
                kwargs["aws_secret_access_key"] = self._config.aws_secret_access_key

            dynamodb = boto3.resource("dynamodb", **kwargs)
            table = dynamodb.Table(self._config.dynamodb_rate_limits_table)
            if getattr(self._config, "dynamodb_endpoint_url", None):
                table.load()
            self._table = table
            return self._table
        except Exception as exc:
            logger.debug("DynamoDB rate limit table unavailable, using in-memory fallback: %s", exc)
            self._dynamo_unavailable = True
            return None

    def is_allowed(self, key: str, max_requests: int, window_seconds: float) -> tuple[bool, int]:
        """Check rate limit atomically via DynamoDB UpdateItem ADD with TTL."""
        table = self._get_table()
        if table is None:
            return self._fallback.is_allowed(key, max_requests, window_seconds)

        now = int(time.time())
        window_sec_int = max(1, int(window_seconds))
        bucket = now // window_sec_int
        pk = f"{key}:{bucket}"
        expires_at = now + (window_sec_int * 2)

        try:
            response = table.update_item(
                Key={"key": pk},
                UpdateExpression="ADD req_count :inc SET expires_at = :exp",
                ExpressionAttributeValues={
                    ":inc": 1,
                    ":exp": expires_at,
                },
                ReturnValues="UPDATED_NEW",
            )
            count = int(response.get("Attributes", {}).get("req_count", 1))
            if count > max_requests:
                elapsed_in_window = now % window_sec_int
                retry_after = max(1, window_sec_int - elapsed_in_window)
                return False, retry_after
            return True, 0
        except (BotoCoreError, ClientError, Exception) as exc:
            logger.debug("DynamoDB rate limit update failed, using in-memory fallback: %s", exc)
            return self._fallback.is_allowed(key, max_requests, window_seconds)

    def reset(self) -> None:
        """Reset rate limiter state (clears in-memory fallback records)."""
        self._dynamo_unavailable = False
        self._fallback.reset()


rate_limiter = DynamoDbRateLimiter()


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


