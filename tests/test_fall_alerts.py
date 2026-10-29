"""Comprehensive unit and integration tests for Fall Alerting lifecycle, contacts CRUD, SMTP escalation, SMS relay, and ACK pages."""

from __future__ import annotations

import dataclasses
import json
import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings
from app.core.pseudonymization import get_subject_pseudonym
from app.core.rate_limit import rate_limiter
from app.db.database import get_db
from app.db.models import (
    AlertContact,
    Base,
    FallAlert,
    FallAlertNotification,
    FallAlertNotificationStatus,
    FallAlertState,
    User,
    UserRole,
)
from app.main import app
from app.schemas.fall_alert import FallAlertIngestPayload
from app.services import token_service
from app.services.aws_iot_service import AWSIoTPublishService
from app.services.email_service import SmtpEmailChannel
from app.services.fall_alert_service import (
    ACK_TOKEN_SALT,
    generate_ack_token,
    ingest_fall_alert_event,
    process_due_fall_alerts,
)


class FakeSMTPServer:
    """In-memory SMTP server spy recording sent EmailMessage objects and supporting simulated failures."""

    def __init__(self, fail_recipients: set[str] | None = None, fail_all: bool = False) -> None:
        self.sent_messages: list[EmailMessage] = []
        self.fail_recipients = {r.lower() for r in (fail_recipients or set())}
        self.fail_all = fail_all
        self.attempts_by_recipient: dict[str, int] = {}

    def __call__(self, host: str, port: int, timeout: float = 10.0) -> "FakeSMTPServer":
        return self

    def __enter__(self) -> "FakeSMTPServer":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    def ehlo(self) -> None:
        pass

    def starttls(self) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        pass

    def send_message(self, msg: EmailMessage) -> None:
        to_header = str(msg.get("To", "")).lower()
        self.attempts_by_recipient[to_header] = self.attempts_by_recipient.get(to_header, 0) + 1
        if self.fail_all or any(rec in to_header for rec in self.fail_recipients):
            raise smtplib.SMTPException(f"Simulated SMTP failure for {to_header}")
        self.sent_messages.append(msg)


class FakeIoTClient:
    """In-memory spy for AWS IoT Data Plane publish calls."""

    def __init__(self) -> None:
        self.published: list[dict] = []
        self.fail_publish: bool = False

    def publish(self, **kwargs) -> None:
        if self.fail_publish:
            raise RuntimeError("Simulated IoT publish failure")
        self.published.append(
            {
                "topic": kwargs["topic"],
                "qos": kwargs.get("qos", 1),
                "payload": json.loads(kwargs["payload"]),
            }
        )


@pytest.fixture(autouse=True)
def reset_rate_limiter_fixture():
    rate_limiter.reset()
    yield
    rate_limiter.reset()


@pytest.fixture()
def test_settings() -> Settings:
    return Settings(
        ingest_token="test-ingest-secret",
        environment="development",
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_user="alerts@healthkicks.test",
        smtp_password="app-password-16ch",
        smtp_from_name="HealthKicks Alertes",
        public_base_url="https://healthkicks.duckdns.org:8443",
        fall_alert_ack_secret="test-ack-secret-key-minimum-32-bytes",
        fall_alert_cancel_window_sec=60,
        fall_alert_escalation_step_sec=180,
        fall_alert_ack_token_ttl_sec=7200,
        fall_alert_replay_threshold_sec=120,
        fall_alert_max_contacts=5,
    )


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def fake_smtp() -> FakeSMTPServer:
    return FakeSMTPServer()


@pytest.fixture()
def fake_iot() -> FakeIoTClient:
    return FakeIoTClient()


@pytest.fixture()
def client(db_session, test_settings, fake_smtp, fake_iot, monkeypatch):
    def override_get_db():
        yield db_session

    email_channel = SmtpEmailChannel(config=test_settings, smtp_factory=fake_smtp)
    iot_pub = AWSIoTPublishService(client=fake_iot, config=test_settings)

    monkeypatch.setattr("app.services.fall_alert_service.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.services.email_service.get_settings", lambda: test_settings)
    monkeypatch.setattr(
        "app.services.email_service.SmtpEmailChannel._create_smtp",
        lambda self, h, p, t=10.0: fake_smtp,
    )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_settings] = lambda: test_settings

    # Inject fake publisher into main app routers where applicable
    monkeypatch.setattr("app.main.publisher._client", fake_iot)
    monkeypatch.setattr("app.main.publisher._config", test_settings)

    with TestClient(app) as test_client:
        test_client.email_channel = email_channel  # type: ignore[attr-defined]
        test_client.iot_publisher = iot_pub  # type: ignore[attr-defined]
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def patient_user(db_session) -> User:
    u = User(
        google_sub="sub-patient-1",
        email="jean.martin@example.com",
        name="Jean Martin",
        role=UserRole.user,
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def clinician_user(db_session) -> User:
    u = User(
        google_sub="sub-clinician-1",
        email="claire.dubois@example.com",
        name="Claire Dubois",
        role=UserRole.clinician,
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def admin_user(db_session) -> User:
    u = User(
        google_sub="sub-admin-1",
        email="admin@example.com",
        name="Admin HealthKicks",
        role=UserRole.admin,
        is_active=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _auth_headers(user: User) -> dict[str, str]:
    token = token_service.issue_access_token(user)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Emergency Contacts CRUD, Phone Validation, Reordering, Compaction, and Test Email/SMS
# ---------------------------------------------------------------------------


def test_alert_contacts_crud_max_limit_and_compaction(client, patient_user) -> None:
    headers = _auth_headers(patient_user)

    # Empty initially
    res_empty = client.get("/api/v1/users/me/alert-contacts", headers=headers)
    assert res_empty.status_code == 200
    assert res_empty.json() == []

    # Create 5 contacts
    created_ids: list[int] = []
    for i in range(1, 6):
        res = client.post(
            "/api/v1/users/me/alert-contacts",
            headers=headers,
            json={
                "first_name": f"  Proche{i}  ",
                "last_name": f"  Famille{i} ",
                "email": f"PROCHE{i}@Example.com",
            },
        )
        assert res.status_code == 201
        body = res.json()
        assert body["first_name"] == f"Proche{i}"
        assert body["last_name"] == f"Famille{i}"
        assert body["email"] == f"proche{i}@example.com"
        assert body["phone"] is None
        assert body["priority"] == i
        created_ids.append(body["id"])

    # 6th contact rejected with 409 Conflict
    res_sixth = client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={
            "first_name": "Proche6",
            "last_name": "Famille6",
            "email": "proche6@example.com",
        },
    )
    assert res_sixth.status_code == 409

    # Delete 2nd contact -> remaining 4 contacts re-compacted to priorities 1..4
    del_res = client.delete(
        f"/api/v1/users/me/alert-contacts/{created_ids[1]}",
        headers=headers,
    )
    assert del_res.status_code == 204

    list_after_del = client.get("/api/v1/users/me/alert-contacts", headers=headers).json()
    assert len(list_after_del) == 4
    assert [c["priority"] for c in list_after_del] == [1, 2, 3, 4]
    assert [c["id"] for c in list_after_del] == [
        created_ids[0],
        created_ids[2],
        created_ids[3],
        created_ids[4],
    ]

    # Duplicate email rejected with 409 Conflict
    res_dup = client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={
            "first_name": "Autre",
            "last_name": "Nom",
            "email": "PROCHE1@example.com",
        },
    )
    assert res_dup.status_code == 409

    # Reorder remaining 4 contacts in reverse order
    reversed_ids = list(reversed([c["id"] for c in list_after_del]))
    res_order = client.put(
        "/api/v1/users/me/alert-contacts/order",
        headers=headers,
        json={"contact_ids": reversed_ids},
    )
    assert res_order.status_code == 200
    ordered = res_order.json()
    assert [c["id"] for c in ordered] == reversed_ids
    assert [c["priority"] for c in ordered] == [1, 2, 3, 4]

    # Invalid reorder payload (missing IDs) -> 422
    res_bad_order = client.put(
        "/api/v1/users/me/alert-contacts/order",
        headers=headers,
        json={"contact_ids": reversed_ids[:2]},
    )
    assert res_bad_order.status_code == 422

    # Patch contact name & email
    target_id = reversed_ids[0]
    res_patch = client.patch(
        f"/api/v1/users/me/alert-contacts/{target_id}",
        headers=headers,
        json={"first_name": "Marie-Claire", "email": "mc.new@example.com"},
    )
    assert res_patch.status_code == 200
    assert res_patch.json()["first_name"] == "Marie-Claire"
    assert res_patch.json()["email"] == "mc.new@example.com"
    assert "uuid" in res_patch.json()

    # Patch via UUID
    contact_uuid = res_patch.json()["uuid"]
    assert contact_uuid is not None
    res_patch_uuid = client.patch(
        f"/api/v1/users/me/alert-contacts/{contact_uuid}",
        headers=headers,
        json={"first_name": "Marie-Claire-Updated"},
    )
    assert res_patch_uuid.status_code == 200
    assert res_patch_uuid.json()["first_name"] == "Marie-Claire-Updated"


def test_alert_contact_phone_normalization_validation_and_clearing(client, patient_user) -> None:
    headers = _auth_headers(patient_user)

    # Create contact with formatted phone number -> normalized to +32470123456
    res_create = client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={
            "first_name": "Marie",
            "last_name": "Dupont",
            "email": "marie.phone@example.com",
            "phone": " +32 (470) 12-34.56 ",
        },
    )
    assert res_create.status_code == 201
    contact_id = res_create.json()["id"]
    assert res_create.json()["phone"] == "+32470123456"

    # Invalid phone rejected with 422
    res_invalid = client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={
            "first_name": "Bad",
            "last_name": "Phone",
            "email": "bad.phone@example.com",
            "phone": "abc-123",
        },
    )
    assert res_invalid.status_code == 422

    # Update phone only
    res_update = client.patch(
        f"/api/v1/users/me/alert-contacts/{contact_id}",
        headers=headers,
        json={"phone": "0471 99 88 77"},
    )
    assert res_update.status_code == 200
    assert res_update.json()["phone"] == "0471998877"

    # Clear phone by passing {"phone": null}
    res_clear = client.patch(
        f"/api/v1/users/me/alert-contacts/{contact_id}",
        headers=headers,
        json={"phone": None},
    )
    assert res_clear.status_code == 200
    assert res_clear.json()["phone"] is None

    # Set phone again then clear with empty string {"phone": "   "}
    client.patch(
        f"/api/v1/users/me/alert-contacts/{contact_id}",
        headers=headers,
        json={"phone": "+33612345678"},
    )
    res_clear_str = client.patch(
        f"/api/v1/users/me/alert-contacts/{contact_id}",
        headers=headers,
        json={"phone": "   "},
    )
    assert res_clear_str.status_code == 200
    assert res_clear_str.json()["phone"] is None


def test_alert_contact_test_email_and_rate_limit(client, patient_user, fake_smtp, fake_iot) -> None:
    headers = _auth_headers(patient_user)
    res_create = client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={
            "first_name": "Sophie",
            "last_name": "Martin",
            "email": "sophie.martin@example.com",
            "phone": "+32470998877",
        },
    )
    assert res_create.status_code == 201
    contact_id = res_create.json()["id"]

    # Send 3 test emails (allowed within 3/hour limit) -> also dispatches TEST_SMS on MQTT
    for _ in range(3):
        res_test = client.post(
            f"/api/v1/users/me/alert-contacts/{contact_id}/test-email",
            headers=headers,
        )
        assert res_test.status_code == 202
        assert res_test.json()["status"] == "sent"
        assert res_test.json()["contact_id"] == contact_id
        assert res_test.json()["sms_dispatched"] is True

    assert len(fake_smtp.sent_messages) == 3
    assert "Email de test" in str(fake_smtp.sent_messages[0]["Subject"])
    assert len(fake_iot.published) == 3
    assert fake_iot.published[-1]["payload"]["state"] == "TEST_SMS"
    assert fake_iot.published[-1]["payload"]["sms_command"]["is_test"] is True
    assert fake_iot.published[-1]["payload"]["sms_command"]["recipient_phone"] == "+32470998877"
    assert "Test SMS" in fake_iot.published[-1]["payload"]["sms_command"]["message"]

    # Verify last_test_email_at was updated
    contacts = client.get("/api/v1/users/me/alert-contacts", headers=headers).json()
    assert contacts[0]["last_test_email_at"] is not None

    # 4th test email within the hour -> 429 Too Many Requests
    res_429 = client.post(
        f"/api/v1/users/me/alert-contacts/{contact_id}/test-email",
        headers=headers,
    )
    assert res_429.status_code == 429


def test_alert_contact_test_email_smtp_failure_returns_502(client, patient_user, fake_smtp) -> None:
    headers = _auth_headers(patient_user)
    res_create = client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={
            "first_name": "Luc",
            "last_name": "Martin",
            "email": "luc.martin@example.com",
        },
    )
    contact_id = res_create.json()["id"]
    fake_smtp.fail_all = True

    res_fail = client.post(
        f"/api/v1/users/me/alert-contacts/{contact_id}/test-email",
        headers=headers,
    )
    assert res_fail.status_code == 502


# ---------------------------------------------------------------------------
# 2. Fall Alert Lifecycle: DETECTED, SUPPRESSED, CANCEL, CONFIRM, Escalation & SMS
# ---------------------------------------------------------------------------


def test_fall_alert_suppressed_when_replayed_or_delayed(
    client, patient_user, fake_smtp, fake_iot
) -> None:
    ingest_headers = {"X-Ingest-Token": "test-ingest-secret"}
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    # 1. delivery="replayed" -> SUPPRESSED
    res_replayed = client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-replayed-001",
            "action": "DETECTED",
            "device_id": "HK-1",
            "event_type": "fall_forward",
            "confidence": 0.92,
            "detected_at": now_ms,
            "delivery": "replayed",
            "sent_at": now_ms,
        },
    )
    assert res_replayed.status_code == 200
    assert res_replayed.json()["state"] == "SUPPRESSED"

    # 2. delivery="live" but detected_at > 120s in the past -> SUPPRESSED
    old_ms = int((datetime.now(UTC) - timedelta(seconds=180)).timestamp() * 1000)
    res_delayed = client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-delayed-002",
            "action": "DETECTED",
            "device_id": "HK-1",
            "event_type": "fall_backward",
            "confidence": 0.88,
            "detected_at": old_ms,
            "delivery": "live",
            "sent_at": now_ms,
        },
    )
    assert res_delayed.status_code == 200
    assert res_delayed.json()["state"] == "SUPPRESSED"
    assert len(fake_smtp.sent_messages) == 0
    assert len(fake_iot.published) == 0


def test_fall_alert_cancel_within_60s_prevents_emails(
    client, db_session, patient_user, fake_smtp, fake_iot, test_settings
) -> None:
    headers = _auth_headers(patient_user)
    ingest_headers = {"X-Ingest-Token": "test-ingest-secret"}

    client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={"first_name": "Marie", "last_name": "Dupont", "email": "marie@example.com"},
    )

    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)

    # 1. DETECTED live -> PENDING
    res_det = client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-cancel-user-01",
            "action": "DETECTED",
            "device_id": "HK-1",
            "event_type": "fall_lateral",
            "confidence": 0.90,
            "detected_at": now_ms,
            "delivery": "live",
            "sent_at": now_ms,
        },
    )
    assert res_det.status_code == 200
    assert res_det.json()["state"] == "PENDING"
    assert len(fake_iot.published) == 1
    assert fake_iot.published[-1]["payload"]["state"] == "PENDING"

    # 2. Patient presses "Je vais bien" (user_cancel) after 15s
    res_cancel = client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-cancel-user-01",
            "action": "CANCEL",
            "cancel_reason": "user_cancel",
            "sent_at": now_ms + 15000,
        },
    )
    assert res_cancel.status_code == 200
    assert res_cancel.json()["state"] == "CANCELLED"
    assert fake_iot.published[-1]["payload"]["state"] == "CANCELLED"
    assert fake_iot.published[-1]["payload"]["cancel_reason"] == "user_cancel"

    # 3. Worker runs after 70s -> no emails sent
    processed = process_due_fall_alerts(
        db_session,
        now_utc=now + timedelta(seconds=70),
        email_channel=client.email_channel,
        iot_publisher=client.iot_publisher,
        config=test_settings,
    )
    assert processed == 0
    assert len(fake_smtp.sent_messages) == 0


def test_fall_alert_sequential_escalation_and_exhaustion(
    client, db_session, patient_user, fake_smtp, fake_iot, test_settings
) -> None:
    headers = _auth_headers(patient_user)
    ingest_headers = {"X-Ingest-Token": "test-ingest-secret"}

    # Configure 2 emergency contacts
    client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={"first_name": "Marie", "last_name": "Dupont", "email": "marie@example.com"},
    )
    client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={"first_name": "Paul", "last_name": "Martin", "email": "paul@example.com"},
    )

    t0 = datetime(2026, 10, 23, 8, 39, 0, tzinfo=UTC)
    t0_ms = int(t0.timestamp() * 1000)

    # 1. Ingest DETECTED at t0
    payload_det = FallAlertIngestPayload(
        user_id=patient_user.id,
        event_id="evt-seq-001",
        action="DETECTED",
        device_id="HK-1",
        event_type="fall_forward",
        confidence=0.95,
        detected_at=t0_ms,
        delivery="live",
        location={"lat": 50.8466, "lon": 4.3528, "accuracy_m": 10.0, "captured_at": t0_ms},
        sent_at=t0_ms,
    )
    res_det = ingest_fall_alert_event(
        db_session,
        payload_det,
        email_channel=client.email_channel,
        iot_publisher=client.iot_publisher,
        config=test_settings,
        now_utc=t0,
    )
    assert res_det.state == "PENDING"
    alert_id = res_det.alert_id
    assert len(fake_smtp.sent_messages) == 0

    # Concurrent DETECTED for same user while PENDING -> duplicate=True, no second alert created
    res_concurrent = client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-seq-concurrent-ignored",
            "action": "DETECTED",
            "device_id": "HK-1",
            "event_type": "fall_forward",
            "confidence": 0.80,
            "detected_at": t0_ms + 5000,
            "delivery": "live",
            "sent_at": t0_ms + 5000,
        },
    )
    assert res_concurrent.status_code == 200
    assert res_concurrent.json()["duplicate"] is True
    assert res_concurrent.json()["alert_id"] == alert_id

    # 2. At t0 + 61s: 60s cancel window expires -> escalates to Contact #1 (Marie)
    t1 = t0 + timedelta(seconds=61)
    processed_1 = process_due_fall_alerts(
        db_session,
        now_utc=t1,
        email_channel=client.email_channel,
        iot_publisher=client.iot_publisher,
        config=test_settings,
    )
    assert processed_1 == 1
    assert len(fake_smtp.sent_messages) == 1
    assert "marie@example.com" in str(fake_smtp.sent_messages[0]["To"])
    assert "Contact 1/2" in str(fake_smtp.sent_messages[0]["Subject"])
    assert (
        "https://www.google.com/maps?q=50.846600,4.352800"
        in fake_smtp.sent_messages[0].get_body(preferencelist=("plain",)).get_content()
    )

    alert = db_session.query(FallAlert).filter_by(id=alert_id).one()
    assert alert.state == FallAlertState.ESCALATING.value
    assert alert.current_contact_rank == 1

    # 3. At t1 + 181s: Contact #1 did not acknowledge -> escalates to Contact #2 (Paul)
    t2 = t1 + timedelta(seconds=181)
    processed_2 = process_due_fall_alerts(
        db_session,
        now_utc=t2,
        email_channel=client.email_channel,
        iot_publisher=client.iot_publisher,
        config=test_settings,
    )
    assert processed_2 == 1
    assert len(fake_smtp.sent_messages) == 2
    assert "paul@example.com" in str(fake_smtp.sent_messages[1]["To"])
    assert "Contact 2/2" in str(fake_smtp.sent_messages[1]["Subject"])

    db_session.refresh(alert)
    assert alert.state == FallAlertState.ESCALATING.value
    assert alert.current_contact_rank == 2

    # 4. At t2 + 181s: Contact #2 did not acknowledge -> transitions to EXHAUSTED (112 fallback)
    t3 = t2 + timedelta(seconds=181)
    processed_3 = process_due_fall_alerts(
        db_session,
        now_utc=t3,
        email_channel=client.email_channel,
        iot_publisher=client.iot_publisher,
        config=test_settings,
    )
    assert processed_3 == 1
    assert len(fake_smtp.sent_messages) == 2  # No further emails sent!

    db_session.refresh(alert)
    assert alert.state == FallAlertState.EXHAUSTED.value
    assert alert.next_action_at_utc is None
    assert alert.closed_at_utc is not None
    assert fake_iot.published[-1]["payload"]["state"] == "EXHAUSTED"


def test_sms_escalation_dual_channel_sms_status_and_sms_ack(
    client, db_session, patient_user, fake_smtp, fake_iot
) -> None:
    headers = _auth_headers(patient_user)
    ingest_headers = {"X-Ingest-Token": "test-ingest-secret"}

    # Contact #1 has both email and phone
    client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={
            "first_name": "Marie",
            "last_name": "Dupont",
            "email": "marie@example.com",
            "phone": "+32470123456",
        },
    )

    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)

    # DETECTED + CONFIRM -> immediate escalation to Contact #1 (Email + SMS)
    client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-sms-dual-01",
            "action": "DETECTED",
            "device_id": "HK-1",
            "event_type": "fall_forward",
            "confidence": 0.94,
            "detected_at": now_ms,
            "delivery": "live",
            "location": {"lat": 50.85, "lon": 4.35, "accuracy_m": 10.0, "captured_at": now_ms},
            "sent_at": now_ms,
        },
    )
    res_confirm = client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-sms-dual-01",
            "action": "CONFIRM",
            "sent_at": now_ms + 1000,
        },
    )
    assert res_confirm.status_code == 200
    assert res_confirm.json()["state"] == "ESCALATING"
    alert_id = res_confirm.json()["alert_id"]

    # Check 2 notifications created for rank 1: email (SENT) and sms (PENDING_SMS)
    notifs = (
        db_session.query(FallAlertNotification)
        .filter_by(alert_id=alert_id)
        .order_by(FallAlertNotification.id.asc())
        .all()
    )
    assert len(notifs) == 2
    email_notif, sms_notif = notifs[0], notifs[1]
    assert email_notif.channel == "email"
    assert email_notif.status == FallAlertNotificationStatus.SENT.value
    assert sms_notif.channel == "sms"
    assert sms_notif.contact_phone == "+32470123456"
    assert sms_notif.status == FallAlertNotificationStatus.PENDING_SMS.value

    # Check MQTT downlink contains sms_command with sms_notif.id
    downlink_payload = fake_iot.published[-1]["payload"]
    assert downlink_payload["state"] == "ESCALATING"
    assert "sms_command" in downlink_payload
    sms_cmd = downlink_payload["sms_command"]
    assert sms_cmd["notification_id"] == sms_notif.id
    assert sms_cmd["recipient_phone"] == "+32470123456"
    assert sms_cmd["is_test"] is False
    assert "[HealthKicks URGENCE]" in sms_cmd["message"]
    assert "/fall-alerts/ack?token=" in sms_cmd["message"]
    assert "https://healthkicks.duckdns.org/fall-alerts/ack?token=" in sms_cmd["message"]

    # Check notified_contacts_count == 1 (distinct rank count, not 2!)
    res_list = client.get("/api/v1/fall-alerts", headers=headers)
    assert res_list.status_code == 200
    assert res_list.json()["items"][0]["notified_contacts_count"] == 1

    # Mobile sends SMS_STATUS = SENT
    res_sms_sent = client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-sms-dual-01",
            "action": "SMS_STATUS",
            "notification_id": sms_notif.id,
            "sms_status": "SENT",
            "sent_at": now_ms + 3000,
        },
    )
    assert res_sms_sent.status_code == 200
    db_session.refresh(sms_notif)
    assert sms_notif.status == FallAlertNotificationStatus.SENT.value

    # Contact clicks the ACK link from the SMS -> transitions alert & SMS notification to ACKNOWLEDGED
    sms_token = sms_cmd["message"].split("token=")[-1].strip()
    res_ack = client.post(
        "/api/v1/fall-alerts/ack",
        json={"token": sms_token},
    )
    assert res_ack.status_code == 200
    db_session.refresh(sms_notif)
    assert sms_notif.status == FallAlertNotificationStatus.ACKNOWLEDGED.value
    assert sms_notif.acknowledged_at_utc is not None

    # Late SMS_STATUS after ACKNOWLEDGED does not overwrite ACKNOWLEDGED status
    client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-sms-dual-01",
            "action": "SMS_STATUS",
            "notification_id": sms_notif.id,
            "sms_status": "FAILED",
            "error_message": "Late failure ignored",
            "sent_at": now_ms + 5000,
        },
    )
    db_session.refresh(sms_notif)
    assert sms_notif.status == FallAlertNotificationStatus.ACKNOWLEDGED.value


def test_sms_status_failed_records_error_message(
    client, db_session, patient_user
) -> None:
    headers = _auth_headers(patient_user)
    ingest_headers = {"X-Ingest-Token": "test-ingest-secret"}

    client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={
            "first_name": "Paul",
            "last_name": "Martin",
            "email": "paul@example.com",
            "phone": "+32471223344",
        },
    )

    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-sms-fail-status",
            "action": "DETECTED",
            "device_id": "HK-1",
            "event_type": "fall_lateral",
            "confidence": 0.90,
            "detected_at": now_ms,
            "delivery": "live",
            "sent_at": now_ms,
        },
    )
    client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-sms-fail-status",
            "action": "CONFIRM",
            "sent_at": now_ms + 1000,
        },
    )

    sms_notif = (
        db_session.query(FallAlertNotification)
        .filter_by(channel="sms")
        .one()
    )

    res_fail = client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-sms-fail-status",
            "action": "SMS_STATUS",
            "notification_id": sms_notif.id,
            "sms_status": "FAILED",
            "error_message": "SEND_SMS permission denied on Android device",
            "sent_at": now_ms + 2000,
        },
    )
    assert res_fail.status_code == 200
    db_session.refresh(sms_notif)
    assert sms_notif.status == FallAlertNotificationStatus.FAILED.value
    assert sms_notif.error_message == "SEND_SMS permission denied on Android device"


def test_smtp_failure_immediately_escalates_to_next_contact(
    client, db_session, patient_user, fake_smtp, test_settings
) -> None:
    headers = _auth_headers(patient_user)
    client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={"first_name": "Broken", "last_name": "Email", "email": "broken@example.com"},
    )
    client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={"first_name": "Working", "last_name": "Email", "email": "working@example.com"},
    )

    # Simulate SMTP failure for broken@example.com
    fake_smtp.fail_recipients.add("broken@example.com")

    t0 = datetime.now(UTC)
    t0_ms = int(t0.timestamp() * 1000)

    ingest_fall_alert_event(
        db_session,
        FallAlertIngestPayload(
            user_id=patient_user.id,
            event_id="evt-smtp-failover",
            action="DETECTED",
            device_id="HK-1",
            event_type="fall_generic",
            confidence=0.89,
            detected_at=t0_ms,
            delivery="live",
            sent_at=t0_ms,
        ),
        email_channel=client.email_channel,
        iot_publisher=client.iot_publisher,
        config=test_settings,
        now_utc=t0,
    )

    # Advance past 60s: should try broken@example.com (2 retries), record FAILED, and immediately send to working@example.com!
    process_due_fall_alerts(
        db_session,
        now_utc=t0 + timedelta(seconds=61),
        email_channel=client.email_channel,
        iot_publisher=client.iot_publisher,
        config=test_settings,
    )

    alert = db_session.query(FallAlert).filter_by(event_id="evt-smtp-failover").one()
    assert alert.state == FallAlertState.ESCALATING.value
    assert alert.current_contact_rank == 2

    notifs = (
        db_session.query(FallAlertNotification)
        .filter_by(alert_id=alert.id)
        .order_by(FallAlertNotification.rank.asc())
        .all()
    )
    assert len(notifs) == 2
    assert notifs[0].rank == 1
    assert notifs[0].status == FallAlertNotificationStatus.FAILED.value
    assert notifs[0].error_message is not None
    assert notifs[1].rank == 2
    assert notifs[1].status == FallAlertNotificationStatus.SENT.value
    assert len(fake_smtp.sent_messages) == 1
    assert "working@example.com" in str(fake_smtp.sent_messages[0]["To"])


def test_fall_recovery_during_escalating_stops_chain(
    client, db_session, patient_user, fake_smtp, test_settings
) -> None:
    headers = _auth_headers(patient_user)
    ingest_headers = {"X-Ingest-Token": "test-ingest-secret"}

    client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={"first_name": "Marie", "last_name": "Dupont", "email": "marie@example.com"},
    )
    client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={"first_name": "Paul", "last_name": "Martin", "email": "paul@example.com"},
    )

    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)

    # 1. DETECTED + immediate CONFIRM -> ESCALATING (email #1 sent to Marie)
    client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-recovery-during-esc",
            "action": "DETECTED",
            "device_id": "HK-1",
            "event_type": "fall_forward",
            "confidence": 0.93,
            "detected_at": now_ms,
            "delivery": "live",
            "sent_at": now_ms,
        },
    )
    res_confirm = client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-recovery-during-esc",
            "action": "CONFIRM",
            "sent_at": now_ms + 5000,
        },
    )
    assert res_confirm.status_code == 200
    assert res_confirm.json()["state"] == "ESCALATING"
    assert len(fake_smtp.sent_messages) == 1

    # 2. Shoe detects fall_recovery (0x13) while ESCALATING -> CANCELLED
    res_recovery = client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-recovery-during-esc",
            "action": "CANCEL",
            "cancel_reason": "fall_recovery",
            "sent_at": now_ms + 30000,
        },
    )
    assert res_recovery.status_code == 200
    assert res_recovery.json()["state"] == "CANCELLED"

    # 3. Worker runs 5 minutes later -> Paul is never emailed
    process_due_fall_alerts(
        db_session,
        now_utc=now + timedelta(minutes=5),
        email_channel=client.email_channel,
        iot_publisher=client.iot_publisher,
        config=test_settings,
    )
    assert len(fake_smtp.sent_messages) == 1


# ---------------------------------------------------------------------------
# 3. Public HTML Acknowledgment (GET without side effects + POST confirmation)
# ---------------------------------------------------------------------------


def test_public_ack_get_is_side_effect_free_and_post_acknowledges(
    client, db_session, patient_user, fake_smtp, fake_iot, test_settings
) -> None:
    headers = _auth_headers(patient_user)
    ingest_headers = {"X-Ingest-Token": "test-ingest-secret"}

    client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={"first_name": "Marie", "last_name": "Dupont", "email": "marie@example.com"},
    )
    client.post(
        "/api/v1/users/me/alert-contacts",
        headers=headers,
        json={"first_name": "Paul", "last_name": "Martin", "email": "paul@example.com"},
    )

    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)

    client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-ack-flow",
            "action": "DETECTED",
            "device_id": "HK-1",
            "event_type": "fall_forward",
            "confidence": 0.91,
            "detected_at": now_ms,
            "delivery": "live",
            "location": {"lat": 48.8566, "lon": 2.3522, "accuracy_m": 15.0, "captured_at": now_ms},
            "sent_at": now_ms,
        },
    )
    client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-ack-flow",
            "action": "CONFIRM",
            "sent_at": now_ms + 2000,
        },
    )

    alert = db_session.query(FallAlert).filter_by(event_id="evt-ack-flow").one()
    notif = db_session.query(FallAlertNotification).filter_by(alert_id=alert.id, rank=1).one()
    token = generate_ack_token(alert.id, notif.id, config=test_settings)

    # 1. GET /api/v1/fall-alerts/ack/info?token=... safe read-only JSON info for the frontend (no side effects)
    res_info = client.get(f"/api/v1/fall-alerts/ack/info?token={token}")
    assert res_info.status_code == 200
    info_data = res_info.json()
    assert info_data["patient_name"] == "Jean Martin"
    assert info_data["contact_name"] == "Marie Dupont"
    assert info_data["state"] == "ESCALATING"
    assert info_data["confidence_pct"] == 91
    assert info_data["lat"] == 48.8566
    assert info_data["lon"] == 2.3522
    assert "google.com/maps" in info_data["maps_url"]

    db_session.refresh(alert)
    assert alert.state == FallAlertState.ESCALATING.value

    # 2. POST /api/v1/fall-alerts/ack (JSON) transitions to ACKNOWLEDGED
    res_post = client.post(
        "/api/v1/fall-alerts/ack",
        json={"token": token},
    )
    assert res_post.status_code == 200
    post_data = res_post.json()
    assert post_data["status"] == "ok"
    assert post_data["state"] == "ACKNOWLEDGED"
    assert post_data["acknowledged_by"] == "Marie Dupont" 

    db_session.refresh(alert)
    db_session.refresh(notif)
    assert alert.state == FallAlertState.ACKNOWLEDGED.value
    assert alert.acknowledged_by_first_name == "Marie"
    assert alert.acknowledged_by_last_name == "Dupont"
    assert alert.next_action_at_utc is None
    assert notif.status == FallAlertNotificationStatus.ACKNOWLEDGED.value
    assert fake_iot.published[-1]["payload"]["state"] == "ACKNOWLEDGED"
    assert fake_iot.published[-1]["payload"]["acknowledged_by"] == {
        "first_name": "Marie",
        "last_name": "Dupont",
    }

    # 3. Subsequent worker tick does NOT email Contact #2 (Paul)
    process_due_fall_alerts(
        db_session,
        now_utc=now + timedelta(minutes=10),
        email_channel=client.email_channel,
        iot_publisher=client.iot_publisher,
        config=test_settings,
    )
    assert len(fake_smtp.sent_messages) == 1

    # 4. Expired or tampered token returns 400 JSON
    res_bad = client.get("/api/v1/fall-alerts/ack/info?token=invalid-token")
    assert res_bad.status_code == 400
    assert "invalide" in res_bad.json()["detail"].lower()

    serializer = URLSafeTimedSerializer(
        secret_key=test_settings.fall_alert_ack_secret,
        salt=ACK_TOKEN_SALT,
    )
    expired_token = serializer.dumps({"a": alert.id, "n": notif.id})
    # Verify max_age enforcement by setting fall_alert_ack_token_ttl_sec to -1
    expired_cfg = dataclasses.replace(test_settings, fall_alert_ack_token_ttl_sec=-1)
    app.dependency_overrides[get_settings] = lambda: expired_cfg
    import app.services.fall_alert_service as fas

    orig_get_settings = fas.get_settings
    fas.get_settings = lambda: expired_cfg
    try:
        res_exp = client.post("/api/v1/fall-alerts/ack", json={"token": expired_token})
        assert res_exp.status_code == 400
        assert "expiré" in res_exp.text
    finally:
        fas.get_settings = orig_get_settings
        app.dependency_overrides[get_settings] = lambda: test_settings


# ---------------------------------------------------------------------------
# 4. History Endpoints, RBAC Isolation (user/clinician vs admin), and GDPR Pseudonymization
# ---------------------------------------------------------------------------


def test_fall_alerts_history_rbac_and_gdpr_pseudonymization(
    client, db_session, patient_user, clinician_user, admin_user
) -> None:
    now = datetime.now(UTC)
    alert_patient = FallAlert(
        event_id="evt-hist-patient",
        user_id=patient_user.id,
        device_id="HK-1",
        event_type="fall_forward",
        confidence_score=0.94,
        delivery="live",
        state=FallAlertState.ACKNOWLEDGED.value,
        lat=50.85,
        lon=4.35,
        accuracy_m=8.0,
        current_contact_rank=1,
        acknowledged_by_first_name="Marie",
        acknowledged_by_last_name="Dupont",
        detected_at_utc=now - timedelta(minutes=10),
        escalation_started_at_utc=now - timedelta(minutes=9),
        closed_at_utc=now - timedelta(minutes=7),
    )
    alert_clinician = FallAlert(
        event_id="evt-hist-clinician",
        user_id=clinician_user.id,
        device_id="HK-2",
        event_type="fall_lateral",
        confidence_score=0.87,
        delivery="live",
        state=FallAlertState.CANCELLED.value,
        cancel_reason="user_cancel",
        detected_at_utc=now - timedelta(minutes=5),
        closed_at_utc=now - timedelta(minutes=4),
    )
    db_session.add_all([alert_patient, alert_clinician])
    db_session.flush()

    notif = FallAlertNotification(
        alert_id=alert_patient.id,
        rank=1,
        channel="sms",
        contact_first_name="Marie",
        contact_last_name="Dupont",
        contact_email="marie.dupont@example.com",
        contact_phone="+32470123456",
        status=FallAlertNotificationStatus.ACKNOWLEDGED.value,
        sent_at_utc=now - timedelta(minutes=9),
        acknowledged_at_utc=now - timedelta(minutes=7),
    )
    db_session.add(notif)
    db_session.commit()

    # 1. Patient only sees their own alert, in clear (is_pseudonymized=False)
    res_pat = client.get("/api/v1/fall-alerts", headers=_auth_headers(patient_user))
    assert res_pat.status_code == 200
    pat_items = res_pat.json()["items"]
    assert len(pat_items) == 1
    assert pat_items[0]["id"] == alert_patient.id
    assert pat_items[0]["is_pseudonymized"] is False
    assert pat_items[0]["user_display_name"] == f"Patient {patient_user.pseudonym}"
    assert pat_items[0]["acknowledged_by"] == {"first_name": "Marie", "last_name": "Dupont"}

    det_pat = client.get(
        f"/api/v1/fall-alerts/{alert_patient.id}",
        headers=_auth_headers(patient_user),
    )
    assert det_pat.status_code == 200
    assert det_pat.json()["notifications"][0]["channel"] == "sms"
    assert det_pat.json()["notifications"][0]["contact_email"] == "marie.dupont@example.com"
    assert det_pat.json()["notifications"][0]["contact_phone"] == "+32470123456"
    assert det_pat.json()["notifications"][0]["contact_last_name"] == "Dupont"

    # 2. Clinician only sees their own alert, and gets 404 on patient's alert
    res_clin = client.get(
        f"/api/v1/fall-alerts?user_id={patient_user.id}",
        headers=_auth_headers(clinician_user),
    )
    assert res_clin.status_code == 200
    clin_items = res_clin.json()["items"]
    assert len(clin_items) == 1
    assert clin_items[0]["id"] == alert_clinician.id

    res_clin_forbidden = client.get(
        f"/api/v1/fall-alerts/{alert_patient.id}",
        headers=_auth_headers(clinician_user),
    )
    assert res_clin_forbidden.status_code == 404

    # 3. Admin sees all alerts, with GDPR pseudonymization applied to third-party alerts
    res_adm = client.get("/api/v1/fall-alerts", headers=_auth_headers(admin_user))
    assert res_adm.status_code == 200
    assert res_adm.json()["total"] == 2

    det_adm = client.get(
        f"/api/v1/fall-alerts/{alert_patient.id}",
        headers=_auth_headers(admin_user),
    )
    assert det_adm.status_code == 200
    adm_body = det_adm.json()
    assert adm_body["is_pseudonymized"] is True
    assert adm_body["user_display_name"].startswith("Patient SUBJ-")
    assert adm_body["acknowledged_by"] == {"first_name": "Marie", "last_name": "D."}
    assert adm_body["notifications"][0]["contact_last_name"] == "D."
    assert adm_body["notifications"][0]["contact_email"] == "m***@e***.com"
    assert adm_body["notifications"][0]["contact_phone"] == "+32*******56"


def test_mobile_direct_sms_status_endpoint(
    client: TestClient,
    db_session: Session,
    patient_user: User,
) -> None:
    """Test that mobile gateway can report SMS status directly via authenticated REST endpoint."""
    headers = _auth_headers(patient_user)
    ingest_headers = {"X-Ingest-Token": "test-ingest-secret"}
    contact = AlertContact(
        user_id=patient_user.id,
        first_name="Sophie",
        last_name="Martin",
        email="sophie@example.com",
        phone="+33612345678",
        priority=1,
    )
    db_session.add(contact)
    db_session.commit()

    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)

    # Ingest fall alert
    payload_detected = {
        "user_id": str(patient_user.id),
        "event_id": "evt-direct-sms-01",
        "action": "DETECTED",
        "device_id": "HK-2",
        "event_type": "fall_forward",
        "confidence": 0.95,
        "detected_at": now_ms,
        "delivery": "live",
        "sent_at": now_ms,
    }
    res_det = client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json=payload_detected,
    )
    assert res_det.status_code == 200
    alert_id = res_det.json()["alert_id"]

    # Confirm early -> triggers escalation
    res_conf = client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.id),
            "event_id": "evt-direct-sms-01",
            "action": "CONFIRM",
            "sent_at": now_ms + 1000,
        },
    )
    assert res_conf.status_code == 200

    # Notification in DB is initially PENDING_SMS
    sms_notif = (
        db_session.query(FallAlertNotification)
        .filter_by(alert_id=alert_id, channel="sms")
        .one()
    )
    assert sms_notif.status == FallAlertNotificationStatus.PENDING_SMS.value

    # Authenticated mobile calls POST /api/v1/fall-alerts/sms-status
    sent_timestamp_ms = now_ms + 2500
    res_status = client.post(
        "/api/v1/fall-alerts/sms-status",
        headers=headers,
        json={
            "notification_id": sms_notif.id,
            "sms_status": "SENT",
            "sent_at": sent_timestamp_ms,
        },
    )
    assert res_status.status_code == 200
    assert res_status.json()["status"] == "ok"

    # Verify status transitioned to SENT in DB
    db_session.refresh(sms_notif)
    assert sms_notif.status == FallAlertNotificationStatus.SENT.value

    # Verify detail endpoint returns SENT
    res_detail = client.get(f"/api/v1/fall-alerts/{alert_id}", headers=headers)
    assert res_detail.status_code == 200
    detail = res_detail.json()
    sms_item = [n for n in detail["notifications"] if n["channel"] == "sms"][0]
    assert sms_item["status"] == "SENT"


def test_fall_alerts_user_id_filter_numeric_and_pseudonym(
    client: TestClient,
    db_session: Session,
    patient_user: User,
    clinician_user: User,
    admin_user: User,
) -> None:
    """Test filtering fall alerts by numeric ID, pseudonym, or unmatched string (200 OK without 422)."""
    now = datetime.now(UTC)
    alert_patient = FallAlert(
        event_id="evt-filter-pat",
        user_id=patient_user.id,
        device_id="HK-1",
        event_type="fall_forward",
        confidence_score=0.92,
        delivery="live",
        state=FallAlertState.ACKNOWLEDGED.value,
        detected_at_utc=now - timedelta(minutes=10),
    )
    alert_clinician = FallAlert(
        event_id="evt-filter-clin",
        user_id=clinician_user.id,
        device_id="HK-2",
        event_type="fall_lateral",
        confidence_score=0.88,
        delivery="live",
        state=FallAlertState.CANCELLED.value,
        cancel_reason="user_cancel",
        detected_at_utc=now - timedelta(minutes=5),
    )
    db_session.add_all([alert_patient, alert_clinician])
    db_session.commit()

    admin_headers = _auth_headers(admin_user)
    pat_pseudo = patient_user.pseudonym

    # 1. Non-matching string parameter returns 200 OK without 422 (empty list)
    res_str = client.get("/api/v1/fall-alerts?user_id=nonexistent_subj_xyz", headers=admin_headers)
    assert res_str.status_code == 200
    assert res_str.json()["total"] == 0
    assert res_str.json()["items"] == []

    res_single_letter = client.get("/api/v1/fall-alerts?user_id=zzzz", headers=admin_headers)
    assert res_single_letter.status_code == 200
    assert res_single_letter.json()["total"] == 0

    # 2. Numeric filter returns only matching user
    res_num = client.get(f"/api/v1/fall-alerts?user_id={patient_user.id}", headers=admin_headers)
    assert res_num.status_code == 200
    num_items = res_num.json()["items"]
    assert len(num_items) >= 1
    assert all(item["user_id"] == patient_user.id for item in num_items)

    # 3. Pseudonym filter returns matching user
    res_pseudo = client.get(f"/api/v1/fall-alerts?pseudonym={pat_pseudo}", headers=admin_headers)
    assert res_pseudo.status_code == 200
    pseudo_items = res_pseudo.json()["items"]
    assert len(pseudo_items) >= 1
    assert all(item["user_id"] == patient_user.id for item in pseudo_items)

    # Also test legacy user_id filter with pseudonym string
    res_pseudo_legacy = client.get(f"/api/v1/fall-alerts?user_id={pat_pseudo}", headers=admin_headers)
    assert res_pseudo_legacy.status_code == 200
    assert len(res_pseudo_legacy.json()["items"]) >= 1

    # Test retrieval by UUID for alert and alert contacts
    res_det_uuid = client.get(f"/api/v1/fall-alerts/{alert_patient.uuid}", headers=admin_headers)
    assert res_det_uuid.status_code == 200
    assert res_det_uuid.json()["id"] == alert_patient.id
    assert res_det_uuid.json()["uuid"] == str(alert_patient.uuid)
    assert res_det_uuid.json()["user_uuid"] == str(patient_user.uuid)
    assert res_det_uuid.json()["user_pseudonym"] == pat_pseudo

    # 4. Check user_display_name systematic pseudonymization
    for item in pseudo_items:
        assert item["user_display_name"].startswith("Patient SUBJ-")
        assert item["subject_pseudonym"] == pat_pseudo


def test_fall_alert_ingest_uuid_and_downlink_topic(
    client, db_session, patient_user, fake_iot
):
    """Verify that fall alert ingest accepts user_id as UUID string and publishes to healthkicks/v1/users/{uuid}/fall-alert/status."""
    ingest_headers = {"X-Ingest-Token": "test-ingest-secret"}
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    fake_iot.published.clear()

    res = client.post(
        "/api/v1/internal/fall-alerts",
        headers=ingest_headers,
        json={
            "user_id": str(patient_user.uuid),
            "event_id": "evt-uuid-test-01",
            "action": "DETECTED",
            "device_id": "HK-UUID-1",
            "event_type": "fall_forward",
            "confidence": 0.95,
            "detected_at": now_ms,
            "delivery": "live",
            "sent_at": now_ms,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["state"] == "PENDING"
    assert data["event_id"] == "evt-uuid-test-01"

    # Verify downlink publish was sent to the user's UUID status topic
    assert len(fake_iot.published) >= 1
    last_pub = fake_iot.published[-1]
    expected_topic = f"healthkicks/v1/users/{patient_user.uuid}/fall-alert/status"
    assert last_pub["topic"] == expected_topic
    assert last_pub["payload"]["state"] == "PENDING"


def test_fall_alert_postgresql_for_update_of_target_compilation(db_session):
    """Verify that FallAlert query with FOR UPDATE in PostgreSQL specifically targets `OF fall_alerts`.

    PostgreSQL forbids FOR UPDATE on the nullable side of an outer join (such as FallAlert.user
    which has lazy='joined'). Using of=FallAlert avoids 'FOR UPDATE cannot be applied to the nullable
    side of an outer join' errors in the escalation worker.
    """
    from sqlalchemy.dialects import postgresql
    from app.db.models import FallAlert
    from app.services.fall_alert_service import ACTIVE_ALERT_STATES

    query = (
        db_session.query(FallAlert)
        .filter(
            FallAlert.state.in_(ACTIVE_ALERT_STATES),
            FallAlert.next_action_at_utc.is_not(None),
        )
        .order_by(FallAlert.next_action_at_utc.asc(), FallAlert.id.asc())
        .with_for_update(of=FallAlert, skip_locked=True)
    )

    compiled_sql = str(query.statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE OF fall_alerts SKIP LOCKED" in compiled_sql
    assert "FOR UPDATE OF users" not in compiled_sql

