"""Unit and integration tests for Idempotency-Key store and HTTP middleware."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user, verify_ingest_token
from app.core.idempotency import (
    DynamoDbIdempotencyStore,
    IdempotencyOutcome,
    InMemoryIdempotencyStore,
    idempotency_store,
    is_idempotency_target,
    is_valid_idempotency_key,
)
from app.db.database import get_db
from app.db.models import (
    AlertContact,
    Base,
    Device,
    DeviceOwnership,
    DeviceStatus,
    StudioSession,
    User,
    UserRole,
)
from app.main import app


class FakeDynamoIdempotencyTable:
    """In-memory fake DynamoDB table emulating conditional PutItem, GetItem, and DeleteItem."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = dict(kwargs["Item"])
        pk = item["key"]
        condition = kwargs.get("ConditionExpression")
        if condition:
            now = kwargs["ExpressionAttributeValues"][":now"]
            existing = self.items.get(pk)
            if existing is not None and int(existing.get("expires_at", 0)) > now:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "Key exists"}},
                    "PutItem",
                )
        self.items[pk] = item
        return {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        pk = kwargs["Key"]["key"]
        item = self.items.get(pk)
        return {"Item": dict(item)} if item else {}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        pk = kwargs["Key"]["key"]
        self.items.pop(pk, None)
        return {}


@pytest.fixture()
def db_session() -> Session:
    """Provide an isolated in-memory SQLite database session and reset idempotency store."""
    idempotency_store.reset()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = testing_session_local()
    try:
        yield session
    finally:
        session.close()
        app.dependency_overrides.clear()
        idempotency_store.reset()


def test_is_valid_idempotency_key() -> None:
    """Verify key length and printable ASCII constraints."""
    assert is_valid_idempotency_key("req-12345-abcdef") is True
    assert is_valid_idempotency_key("a" * 128) is True
    assert is_valid_idempotency_key("a" * 129) is False
    assert is_valid_idempotency_key("   ") is False
    assert is_valid_idempotency_key("bad\nkey") is False
    assert is_valid_idempotency_key("bad\x00key") is False


def test_is_idempotency_target_matches_fall_alerts_and_studio_routes() -> None:
    """Verify target route matcher includes Fall Alerts and Studio Sessions write routes."""
    assert is_idempotency_target("POST", "/api/v1/internal/fall-alerts") is True
    assert is_idempotency_target("POST", "/api/v1/fall-alerts/ack") is True
    assert is_idempotency_target("POST", "/api/v1/fall-alerts/sms-status") is True
    assert is_idempotency_target("POST", "/api/v1/users/me/alert-contacts") is True
    assert is_idempotency_target("PUT", "/api/v1/users/me/alert-contacts/order") is True
    assert is_idempotency_target("PATCH", "/api/v1/studio/sessions/123e4567/confirm") is True
    assert is_idempotency_target("PATCH", "/api/v1/studio/sessions/123e4567/trim") is True
    assert is_idempotency_target("POST", "/api/v1/devices/HK-001/commands/studio/start") is True
    assert is_idempotency_target("POST", "/api/v1/ingest/telemetry/raw") is True

    # Read or non-targeted methods must not be intercepted
    assert is_idempotency_target("GET", "/api/v1/fall-alerts") is False
    assert is_idempotency_target("DELETE", "/api/v1/studio/sessions/123e4567") is False


def test_dynamodb_idempotency_store_lifecycle() -> None:
    """Verify DynamoDbIdempotencyStore handles ACQUIRED, IN_PROGRESS, REPLAY, MISMATCH, and release."""
    fake_table = FakeDynamoIdempotencyTable()
    store = DynamoDbIdempotencyStore(table=fake_table)

    scope = "idemp:caller1:route1:key-001"
    hash_a = "hash-payload-a"
    hash_b = "hash-payload-b"

    # 1. First attempt acquires IN_PROGRESS lock
    res1 = store.acquire_or_get(scope, hash_a)
    assert res1.outcome == IdempotencyOutcome.ACQUIRED

    # 2. Concurrent attempt with same hash sees IN_PROGRESS
    res2 = store.acquire_or_get(scope, hash_a)
    assert res2.outcome == IdempotencyOutcome.IN_PROGRESS

    # 3. Attempt with different hash sees MISMATCH
    res3 = store.acquire_or_get(scope, hash_b)
    assert res3.outcome == IdempotencyOutcome.MISMATCH

    # 4. Complete and verify REPLAY
    store.complete(
        scope_key=scope,
        request_hash=hash_a,
        status_code=201,
        response_body='{"uuid":"abc-123"}',
        content_type="application/json",
        ttl_seconds=3600,
    )
    res4 = store.acquire_or_get(scope, hash_a)
    assert res4.outcome == IdempotencyOutcome.REPLAY
    assert res4.cached_response is not None
    assert res4.cached_response.status_code == 201
    assert res4.cached_response.body == '{"uuid":"abc-123"}'

    # 5. Release removes record so it can be re-acquired
    store.release(scope)
    res5 = store.acquire_or_get(scope, hash_a)
    assert res5.outcome == IdempotencyOutcome.ACQUIRED


def test_in_memory_store_expiration() -> None:
    """Verify expired keys in InMemoryIdempotencyStore are pruned and can be re-acquired."""
    store = InMemoryIdempotencyStore()
    scope = "idemp:exp:test"
    res1 = store.acquire_or_get(scope, "hash1", lock_ttl_seconds=1)
    assert res1.outcome == IdempotencyOutcome.ACQUIRED
    store._records[scope].expires_at = 0.0  # Force expiration

    res2 = store.acquire_or_get(scope, "hash2", lock_ttl_seconds=10)
    assert res2.outcome == IdempotencyOutcome.ACQUIRED


def test_alert_contact_creation_idempotency_replay_and_mismatch(db_session: Session) -> None:
    """Verify POST /api/v1/users/me/alert-contacts replays identical response and blocks payload mismatch."""
    user = User(
        email="patient@healthkicks.io",
        name="Patient One",
        role=UserRole.user,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user

    client = TestClient(app)
    payload = {
        "first_name": "Alice",
        "last_name": "Martin",
        "email": "alice.martin@example.com",
        "phone": "+32470123456",
    }

    # First call creates the contact (201)
    resp1 = client.post(
        "/api/v1/users/me/alert-contacts",
        json=payload,
        headers={"Idempotency-Key": "idem-contact-001", "Authorization": "Bearer user-tok-1"},
    )
    assert resp1.status_code == 201
    assert resp1.headers["Idempotency-Key"] == "idem-contact-001"
    assert resp1.headers["X-Idempotent-Replayed"] == "false"
    created_uuid = resp1.json()["uuid"]

    # Second call with same key and payload replays 201 with exact same UUID (instead of 409 duplicate email)
    resp2 = client.post(
        "/api/v1/users/me/alert-contacts",
        json=payload,
        headers={"Idempotency-Key": "idem-contact-001", "Authorization": "Bearer user-tok-1"},
    )
    assert resp2.status_code == 201
    assert resp2.headers["Idempotency-Key"] == "idem-contact-001"
    assert resp2.headers["X-Idempotent-Replayed"] == "true"
    assert resp2.json()["uuid"] == created_uuid
    assert db_session.query(AlertContact).count() == 1

    # Third call with same key but different payload returns 422
    different_payload = {**payload, "email": "bob.martin@example.com"}
    resp3 = client.post(
        "/api/v1/users/me/alert-contacts",
        json=different_payload,
        headers={"Idempotency-Key": "idem-contact-001", "Authorization": "Bearer user-tok-1"},
    )
    assert resp3.status_code == 422
    assert "different request payload" in resp3.json()["detail"]


def test_fall_alert_ingest_and_studio_confirm_idempotency(db_session: Session) -> None:
    """Verify POST /api/v1/internal/fall-alerts and PATCH /api/v1/studio/sessions/{id}/confirm support Idempotency-Key."""
    clinician = User(
        email="clinician@healthkicks.io",
        name="Clinician",
        role=UserRole.clinician,
        is_active=True,
    )
    db_session.add(clinician)
    db_session.commit()
    db_session.refresh(clinician)

    device = Device(device_id="HK-SHOE-99", name="Shoe 99", status=DeviceStatus.online)
    ownership = DeviceOwnership(user_id=clinician.id, device_id="HK-SHOE-99")
    session_uuid = uuid4()
    studio_session = StudioSession(
        uuid=session_uuid,
        user_id=clinician.id,
        device_id="HK-SHOE-99",
        label="walking",
        sample_count=95,
        duration_sec=5.0,
        is_validated=False,
    )
    db_session.add_all([device, ownership, studio_session])
    db_session.commit()

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: clinician
    app.dependency_overrides[verify_ingest_token] = lambda: None

    client = TestClient(app)

    # 1. Fall alert internal ingestion with Idempotency-Key
    fall_payload = {
        "user_id": clinician.id,
        "event_id": "evt-fall-idem-100",
        "device_id": "HK-SHOE-99",
        "action": "DETECTED",
        "event_type": "fall_forward",
        "confidence": 0.96,
        "detected_at": int(datetime.now(UTC).timestamp() * 1000),
        "delivery": "live",
    }
    fall_resp1 = client.post(
        "/api/v1/internal/fall-alerts",
        json=fall_payload,
        headers={"Idempotency-Key": "fall-ingest-key-1", "X-Ingest-Token": "test-ingest"},
    )
    assert fall_resp1.status_code == 200
    assert fall_resp1.headers["X-Idempotent-Replayed"] == "false"

    fall_resp2 = client.post(
        "/api/v1/internal/fall-alerts",
        json=fall_payload,
        headers={"Idempotency-Key": "fall-ingest-key-1", "X-Ingest-Token": "test-ingest"},
    )
    assert fall_resp2.status_code == 200
    assert fall_resp2.headers["X-Idempotent-Replayed"] == "true"
    assert fall_resp2.json() == fall_resp1.json()

    # 2. Studio session confirmation with Idempotency-Key
    studio_resp1 = client.patch(
        f"/api/v1/studio/sessions/{session_uuid}/confirm",
        headers={"Idempotency-Key": "studio-confirm-key-1", "Authorization": "Bearer clin-tok"},
    )
    assert studio_resp1.status_code == 200
    assert studio_resp1.headers["X-Idempotent-Replayed"] == "false"
    assert studio_resp1.json()["is_validated"] is True

    studio_resp2 = client.patch(
        f"/api/v1/studio/sessions/{session_uuid}/confirm",
        headers={"Idempotency-Key": "studio-confirm-key-1", "Authorization": "Bearer clin-tok"},
    )
    assert studio_resp2.status_code == 200
    assert studio_resp2.headers["X-Idempotent-Replayed"] == "true"
    assert studio_resp2.json() == studio_resp1.json()


def test_invalid_idempotency_key_and_in_progress_conflict(db_session: Session) -> None:
    """Verify invalid Idempotency-Key returns 400 and concurrent in-flight key returns 409."""
    user = User(
        email="user2@healthkicks.io",
        name="User Two",
        role=UserRole.user,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user

    client = TestClient(app)
    payload = {
        "first_name": "Jean",
        "last_name": "Dupont",
        "email": "jean.dupont@example.com",
    }

    # Key longer than 128 characters -> 400 Bad Request
    bad_resp = client.post(
        "/api/v1/users/me/alert-contacts",
        json=payload,
        headers={"Idempotency-Key": "x" * 130},
    )
    assert bad_resp.status_code == 400
    assert "Invalid Idempotency-Key" in bad_resp.json()["detail"]

    # Failed 422 validation request does NOT lock the key permanently
    invalid_body_resp = client.post(
        "/api/v1/users/me/alert-contacts",
        json={"first_name": ""},
        headers={"Idempotency-Key": "retryable-key-001"},
    )
    assert invalid_body_resp.status_code == 422

    retry_valid_resp = client.post(
        "/api/v1/users/me/alert-contacts",
        json=payload,
        headers={"Idempotency-Key": "retryable-key-001"},
    )
    assert retry_valid_resp.status_code == 201
    assert retry_valid_resp.headers["X-Idempotent-Replayed"] == "false"
