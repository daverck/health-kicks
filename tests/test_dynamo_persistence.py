"""Unit tests for DynamoDB-backed OAuth tickets and distributed rate limiting with TTL."""

import time
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from app.core.rate_limit import DynamoDbRateLimiter, SlidingWindowRateLimiter
from app.services.oauth_ticket_service import OAuthTicketStore


def test_oauth_ticket_store_dynamo_success() -> None:
    mock_table = MagicMock()
    store = OAuthTicketStore(table=mock_table, ttl_seconds=60)

    user_info = {
        "id": 42,
        "email": "user@example.com",
        "name": "Jane Doe",
        "avatar_url": None,
        "role": "user",
        "is_active": True,
    }

    # Create ticket
    ticket = store.create_ticket(
        access_token="acc-tok-123",
        refresh_token="ref-tok-456",
        user=user_info,
        code_challenge="challenge-abc",
    )
    assert ticket is not None
    assert len(ticket) >= 32
    mock_table.put_item.assert_called_once()
    put_args = mock_table.put_item.call_args[1]["Item"]
    assert put_args["ticket"] == ticket
    assert put_args["access_token"] == "acc-tok-123"
    assert put_args["expires_at"] > int(time.time())

    # Consume ticket successfully
    mock_table.delete_item.return_value = {
        "Attributes": {
            "ticket": ticket,
            "access_token": "acc-tok-123",
            "refresh_token": "ref-tok-456",
            "user": user_info,
            "code_challenge": "challenge-abc",
            "expires_at": int(time.time()) + 60,
            "created_at": str(time.time()),
        }
    }
    payload = store.consume_ticket(ticket)
    assert payload is not None
    assert payload["access_token"] == "acc-tok-123"
    assert payload["user"]["email"] == "user@example.com"
    mock_table.delete_item.assert_called_once_with(
        Key={"ticket": ticket},
        ReturnValues="ALL_OLD",
    )

    # Replay attack: second consumption returns empty attributes
    mock_table.delete_item.return_value = {}
    replay = store.consume_ticket(ticket)
    assert replay is None


def test_oauth_ticket_store_dynamo_expired() -> None:
    mock_table = MagicMock()
    store = OAuthTicketStore(table=mock_table, ttl_seconds=60)

    # Returned ticket with expires_at in the past
    mock_table.delete_item.return_value = {
        "Attributes": {
            "ticket": "expired-ticket",
            "access_token": "acc",
            "refresh_token": "ref",
            "user": {},
            "expires_at": int(time.time()) - 10,
        }
    }
    payload = store.consume_ticket("expired-ticket")
    assert payload is None


def test_oauth_ticket_store_fallback_on_dynamo_error() -> None:
    mock_table = MagicMock()
    mock_table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
        "PutItem",
    )
    mock_table.delete_item.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
        "DeleteItem",
    )

    store = OAuthTicketStore(table=mock_table, ttl_seconds=60)
    user_info = {"id": 1, "email": "fallback@example.com"}

    ticket = store.create_ticket(
        access_token="acc",
        refresh_token="ref",
        user=user_info,
    )
    assert ticket is not None

    # Fallback to in-memory store
    payload = store.consume_ticket(ticket)
    assert payload is not None
    assert payload["access_token"] == "acc"

    # Single-use: cannot be consumed twice
    assert store.consume_ticket(ticket) is None


def test_dynamodb_rate_limiter_allows_and_blocks() -> None:
    mock_table = MagicMock()
    limiter = DynamoDbRateLimiter(table=mock_table)

    # 1st request: count = 1 <= 2
    mock_table.update_item.return_value = {"Attributes": {"req_count": 1}}
    allowed, retry_after = limiter.is_allowed("test:user1", max_requests=2, window_seconds=60.0)
    assert allowed is True
    assert retry_after == 0

    # 2nd request: count = 2 <= 2
    mock_table.update_item.return_value = {"Attributes": {"req_count": 2}}
    allowed, retry_after = limiter.is_allowed("test:user1", max_requests=2, window_seconds=60.0)
    assert allowed is True
    assert retry_after == 0

    # 3rd request: count = 3 > 2 (exceeded)
    mock_table.update_item.return_value = {"Attributes": {"req_count": 3}}
    allowed, retry_after = limiter.is_allowed("test:user1", max_requests=2, window_seconds=60.0)
    assert allowed is False
    assert retry_after >= 1


def test_dynamodb_rate_limiter_fallback_on_error() -> None:
    mock_table = MagicMock()
    mock_table.update_item.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "Access Denied"}},
        "UpdateItem",
    )

    fallback = SlidingWindowRateLimiter()
    limiter = DynamoDbRateLimiter(table=mock_table, fallback_limiter=fallback)

    # Should fall back to in-memory sliding window limiter transparently
    assert limiter.is_allowed("key1", max_requests=2, window_seconds=10.0)[0] is True
    assert limiter.is_allowed("key1", max_requests=2, window_seconds=10.0)[0] is True
    # 3rd request blocked by fallback
    assert limiter.is_allowed("key1", max_requests=2, window_seconds=10.0)[0] is False

    limiter.reset()
    assert limiter.is_allowed("key1", max_requests=2, window_seconds=10.0)[0] is True
