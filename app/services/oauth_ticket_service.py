"""Multi-worker persistent OAuth ticket store backed by Amazon DynamoDB with TTL and in-memory fallback."""

import logging
import secrets
import threading
import time
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings, settings

logger = logging.getLogger(__name__)

TICKET_TTL_SECONDS = 60


class OAuthTicketStore:
    """Thread-safe and multi-worker persistent OAuth ticket store with TTL."""

    def __init__(
        self,
        table: Any | None = None,
        config: Settings = settings,
        ttl_seconds: int = TICKET_TTL_SECONDS,
    ) -> None:
        self._table = table
        self._config = config
        self._ttl_seconds = ttl_seconds
        self._memory_lock = threading.Lock()
        self._memory_store: dict[str, dict[str, Any]] = {}
        self._dynamo_unavailable = False

    def _get_table(self) -> Any | None:
        if self._dynamo_unavailable:
            return None
        if self._table is not None:
            return self._table
        try:
            dynamodb = boto3.resource("dynamodb", region_name=self._config.aws_region)
            self._table = dynamodb.Table(self._config.dynamodb_oauth_tickets_table)
            return self._table
        except Exception as exc:
            logger.debug("DynamoDB ticket table unavailable, using in-memory store: %s", exc)
            self._dynamo_unavailable = True
            return None

    def create_ticket(
        self,
        access_token: str,
        refresh_token: str,
        user: Any,
        code_challenge: str | None = None,
    ) -> str:
        """Create a single-use authentication ticket with TTL."""
        ticket = secrets.token_urlsafe(32)
        now = time.time()
        expires_at = int(now) + self._ttl_seconds

        user_data = user.model_dump() if hasattr(user, "model_dump") else user

        table = self._get_table()
        if table is not None:
            try:
                table.put_item(
                    Item={
                        "ticket": ticket,
                        "access_token": access_token,
                        "refresh_token": refresh_token,
                        "user": user_data,
                        "code_challenge": code_challenge or "",
                        "created_at": str(now),
                        "expires_at": expires_at,
                    }
                )
                return ticket
            except (BotoCoreError, ClientError, Exception) as exc:
                logger.debug("Failed to store OAuth ticket in DynamoDB, falling back to memory: %s", exc)

        with self._memory_lock:
            # Prune expired keys
            expired = [k for k, v in self._memory_store.items() if now > v.get("expires_at", 0)]
            for k in expired:
                self._memory_store.pop(k, None)

            self._memory_store[ticket] = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "user": user_data,
                "code_challenge": code_challenge,
                "created_at": now,
                "expires_at": expires_at,
            }
        return ticket

    def consume_ticket(self, ticket: str) -> dict[str, Any] | None:
        """Atomically consume (delete and return) a single-use ticket.

        Returns the ticket payload if valid and not expired, or None.
        """
        now = time.time()
        table = self._get_table()
        if table is not None:
            try:
                response = table.delete_item(
                    Key={"ticket": ticket},
                    ReturnValues="ALL_OLD",
                )
                attributes = response.get("Attributes")
                if attributes:
                    exp = float(attributes.get("expires_at", 0))
                    if now > exp:
                        logger.warning("Attempted to consume expired OAuth ticket: %s", ticket)
                        return None
                    return {
                        "access_token": attributes["access_token"],
                        "refresh_token": attributes["refresh_token"],
                        "user": attributes["user"],
                        "code_challenge": attributes.get("code_challenge") or None,
                        "created_at": float(attributes.get("created_at", now)),
                    }
            except (BotoCoreError, ClientError, Exception) as exc:
                logger.debug("Failed to consume ticket from DynamoDB, checking memory store: %s", exc)

        with self._memory_lock:
            entry = self._memory_store.pop(ticket, None)
            if entry is None:
                return None
            if now > entry.get("expires_at", 0):
                return None
            return entry

    def reset(self) -> None:
        """Clear local in-memory store (for test isolation)."""
        with self._memory_lock:
            self._memory_store.clear()


oauth_ticket_store = OAuthTicketStore()

