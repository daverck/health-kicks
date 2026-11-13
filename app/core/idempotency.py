"""Idempotency-Key store and HTTP middleware helpers for Fall Alerts and Studio Sessions."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Request

from app.core.config import Settings, settings

logger = logging.getLogger("healthkicks.idempotency")

IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENT_REPLAYED_HEADER = "X-Idempotent-Replayed"
MAX_IDEMPOTENCY_KEY_LEN = 128
IN_PROGRESS_LOCK_TTL_SECONDS = 60

_VALID_KEY_RE = re.compile(r"^[\x21-\x7E][\x20-\x7E]{0,127}$")
_STUDIO_START_PATH_RE = re.compile(r"^/api/v1/devices/[^/]+/commands/studio/start/?$")

IDEMPOTENT_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH"})
IDEMPOTENT_PATH_PREFIXES = (
    "/api/v1/internal/fall-alerts",
    "/api/v1/fall-alerts",
    "/api/v1/users/me/alert-contacts",
    "/api/v1/studio/sessions",
    "/api/v1/steps",
    "/api/v1/ingest/telemetry/raw",
    "/api/v1/ingest/event/telemetry/raw",
    "/api/v1/ingest/event",
)


class IdempotencyOutcome(str, Enum):
    """Resolution outcome when evaluating an incoming Idempotency-Key."""

    ACQUIRED = "ACQUIRED"
    REPLAY = "REPLAY"
    IN_PROGRESS = "IN_PROGRESS"
    MISMATCH = "MISMATCH"


@dataclass(slots=True)
class CachedIdempotentResponse:
    """Cached HTTP response stored for a completed idempotent request."""

    status_code: int
    body: str
    content_type: str


@dataclass(slots=True)
class IdempotencyResolution:
    """Outcome of attempting to acquire or replay an idempotency key."""

    outcome: IdempotencyOutcome
    cached_response: CachedIdempotentResponse | None = None


@dataclass(slots=True)
class _InMemoryRecord:
    """Internal record stored by InMemoryIdempotencyStore."""

    request_hash: str
    status: str  # "IN_PROGRESS" | "COMPLETED"
    expires_at: float
    status_code: int = 200
    response_body: str = ""
    content_type: str = "application/json"


def is_valid_idempotency_key(raw_key: str) -> bool:
    """Validate that an Idempotency-Key is 1..128 printable ASCII characters."""
    stripped = raw_key.strip()
    if not stripped or len(stripped) > MAX_IDEMPOTENCY_KEY_LEN:
        return False
    return bool(_VALID_KEY_RE.match(stripped))


def is_idempotency_target(method: str, path: str) -> bool:
    """Return True if the HTTP method and path support Idempotency-Key enforcement."""
    if method.upper() not in IDEMPOTENT_HTTP_METHODS:
        return False
    if any(path.startswith(prefix) for prefix in IDEMPOTENT_PATH_PREFIXES):
        return True
    return bool(_STUDIO_START_PATH_RE.match(path))


def compute_request_scope(request: Request, idempotency_key: str) -> str:
    """Build a deterministic caller + route + key scope identifier."""
    auth_header = (
        request.headers.get("authorization")
        or request.headers.get("x-ingest-token")
        or "public"
    )
    caller_digest = hashlib.sha256(auth_header.encode("utf-8")).hexdigest()[:24]
    route_sig = f"{request.method.upper()}:{request.url.path}"
    route_digest = hashlib.sha256(route_sig.encode("utf-8")).hexdigest()[:16]
    return f"idemp:{caller_digest}:{route_digest}:{idempotency_key.strip()}"


def compute_payload_hash(body: bytes) -> str:
    """Compute SHA-256 hex digest of the raw request payload."""
    return hashlib.sha256(body).hexdigest()


class InMemoryIdempotencyStore:
    """Thread-safe in-memory idempotency store with TTL expiration."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, _InMemoryRecord] = {}

    def _prune_expired_locked(self, now: float) -> None:
        expired_keys = [k for k, rec in self._records.items() if rec.expires_at <= now]
        for k in expired_keys:
            del self._records[k]

    def acquire_or_get(
        self,
        scope_key: str,
        request_hash: str,
        lock_ttl_seconds: int = IN_PROGRESS_LOCK_TTL_SECONDS,
    ) -> IdempotencyResolution:
        """Atomically acquire an IN_PROGRESS lock or return existing REPLAY/MISMATCH/IN_PROGRESS status."""
        now = time.monotonic()
        with self._lock:
            self._prune_expired_locked(now)
            existing = self._records.get(scope_key)
            if existing is not None:
                if existing.request_hash != request_hash:
                    return IdempotencyResolution(outcome=IdempotencyOutcome.MISMATCH)
                if existing.status == "IN_PROGRESS":
                    return IdempotencyResolution(outcome=IdempotencyOutcome.IN_PROGRESS)
                return IdempotencyResolution(
                    outcome=IdempotencyOutcome.REPLAY,
                    cached_response=CachedIdempotentResponse(
                        status_code=existing.status_code,
                        body=existing.response_body,
                        content_type=existing.content_type,
                    ),
                )

            self._records[scope_key] = _InMemoryRecord(
                request_hash=request_hash,
                status="IN_PROGRESS",
                expires_at=now + max(1, lock_ttl_seconds),
            )
            return IdempotencyResolution(outcome=IdempotencyOutcome.ACQUIRED)

    def complete(
        self,
        scope_key: str,
        request_hash: str,
        status_code: int,
        response_body: str,
        content_type: str,
        ttl_seconds: int,
    ) -> None:
        """Transition an acquired key to COMPLETED and cache its response payload."""
        now = time.monotonic()
        with self._lock:
            self._records[scope_key] = _InMemoryRecord(
                request_hash=request_hash,
                status="COMPLETED",
                expires_at=now + max(1, ttl_seconds),
                status_code=status_code,
                response_body=response_body,
                content_type=content_type,
            )

    def release(self, scope_key: str) -> None:
        """Remove an in-progress record when downstream processing fails so retries can proceed."""
        with self._lock:
            self._records.pop(scope_key, None)

    def reset(self) -> None:
        """Clear all stored idempotency records (for unit test isolation)."""
        with self._lock:
            self._records.clear()


class DynamoDbIdempotencyStore:
    """Multi-worker distributed idempotency store backed by DynamoDB with in-memory fallback."""

    def __init__(
        self,
        table: Any | None = None,
        config: Settings = settings,
        fallback_store: InMemoryIdempotencyStore | None = None,
    ) -> None:
        self._table = table
        self._config = config
        self._fallback = fallback_store or InMemoryIdempotencyStore()
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
            if getattr(self._config, "aws_access_key_id", None) and getattr(
                self._config, "aws_secret_access_key", None
            ):
                kwargs["aws_access_key_id"] = self._config.aws_access_key_id
                kwargs["aws_secret_access_key"] = self._config.aws_secret_access_key

            dynamodb = boto3.resource("dynamodb", **kwargs)
            table_name = getattr(
                self._config,
                "dynamodb_idempotency_table",
                self._config.dynamodb_rate_limits_table,
            )
            table = dynamodb.Table(table_name)
            if getattr(self._config, "dynamodb_endpoint_url", None):
                table.load()
            self._table = table
            return self._table
        except Exception as exc:
            logger.debug("DynamoDB idempotency table unavailable, using in-memory fallback: %s", exc)
            self._dynamo_unavailable = True
            return None

    def acquire_or_get(
        self,
        scope_key: str,
        request_hash: str,
        lock_ttl_seconds: int = IN_PROGRESS_LOCK_TTL_SECONDS,
    ) -> IdempotencyResolution:
        """Atomically acquire a distributed lock or read cached idempotent response from DynamoDB."""
        table = self._get_table()
        if table is None:
            return self._fallback.acquire_or_get(scope_key, request_hash, lock_ttl_seconds)

        now = int(time.time())
        expires_at = now + max(1, int(lock_ttl_seconds))
        try:
            table.put_item(
                Item={
                    "key": scope_key,
                    "request_hash": request_hash,
                    "status": "IN_PROGRESS",
                    "expires_at": expires_at,
                },
                ConditionExpression="attribute_not_exists(#k) OR expires_at <= :now",
                ExpressionAttributeNames={"#k": "key"},
                ExpressionAttributeValues={":now": now},
            )
            return IdempotencyResolution(outcome=IdempotencyOutcome.ACQUIRED)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code != "ConditionalCheckFailedException":
                logger.warning("DynamoDB idempotency put_item error (%s), falling back to memory", error_code)
                self._dynamo_unavailable = True
                return self._fallback.acquire_or_get(scope_key, request_hash, lock_ttl_seconds)

            try:
                resp = table.get_item(Key={"key": scope_key}, ConsistentRead=True)
                item = resp.get("Item")
            except (BotoCoreError, ClientError) as get_exc:
                logger.warning("DynamoDB idempotency get_item failed (%s), falling back to memory", get_exc)
                self._dynamo_unavailable = True
                return self._fallback.acquire_or_get(scope_key, request_hash, lock_ttl_seconds)

            if not item or int(item.get("expires_at", 0)) <= now:
                return self._fallback.acquire_or_get(scope_key, request_hash, lock_ttl_seconds)

            stored_hash = str(item.get("request_hash", ""))
            if stored_hash != request_hash:
                return IdempotencyResolution(outcome=IdempotencyOutcome.MISMATCH)

            if item.get("status") == "IN_PROGRESS":
                return IdempotencyResolution(outcome=IdempotencyOutcome.IN_PROGRESS)

            return IdempotencyResolution(
                outcome=IdempotencyOutcome.REPLAY,
                cached_response=CachedIdempotentResponse(
                    status_code=int(item.get("status_code", 200)),
                    body=str(item.get("response_body", "")),
                    content_type=str(item.get("content_type", "application/json")),
                ),
            )
        except Exception as exc:
            logger.warning("DynamoDB idempotency error (%s), falling back to memory", exc)
            self._dynamo_unavailable = True
            return self._fallback.acquire_or_get(scope_key, request_hash, lock_ttl_seconds)

    def complete(
        self,
        scope_key: str,
        request_hash: str,
        status_code: int,
        response_body: str,
        content_type: str,
        ttl_seconds: int | None = None,
    ) -> None:
        """Persist COMPLETED response in DynamoDB (or fallback store)."""
        effective_ttl = int(
            ttl_seconds
            if ttl_seconds is not None
            else getattr(self._config, "idempotency_ttl_seconds", 86400)
        )
        table = self._get_table()
        if table is None:
            self._fallback.complete(
                scope_key, request_hash, status_code, response_body, content_type, effective_ttl
            )
            return

        now = int(time.time())
        expires_at = now + max(1, effective_ttl)
        try:
            table.put_item(
                Item={
                    "key": scope_key,
                    "request_hash": request_hash,
                    "status": "COMPLETED",
                    "status_code": status_code,
                    "response_body": response_body,
                    "content_type": content_type,
                    "expires_at": expires_at,
                }
            )
        except Exception as exc:
            logger.warning("DynamoDB idempotency complete failed (%s), using fallback", exc)
            self._dynamo_unavailable = True
            self._fallback.complete(
                scope_key, request_hash, status_code, response_body, content_type, effective_ttl
            )

    def release(self, scope_key: str) -> None:
        """Delete an IN_PROGRESS lock when the downstream request fails."""
        table = self._get_table()
        if table is None:
            self._fallback.release(scope_key)
            return
        try:
            table.delete_item(Key={"key": scope_key})
        except Exception as exc:
            logger.debug("DynamoDB idempotency release failed (%s)", exc)
            self._dynamo_unavailable = True
            self._fallback.release(scope_key)

    def reset(self) -> None:
        """Reset in-memory fallback records."""
        self._fallback.reset()


idempotency_store = DynamoDbIdempotencyStore()
