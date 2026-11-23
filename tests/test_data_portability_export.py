"""Integration and unit tests for GDPR Article 20 Data Portability export."""

import io
import json
import uuid
import zipfile
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import get_db
from app.db.models import (
    AlertContact,
    Base,
    DailyActivityStep,
    Device,
    DeviceOwnership,
    DeviceStatus,
    FallAlert,
    FallAlertDelivery,
    FallAlertNotification,
    FallAlertNotificationStatus,
    FallAlertState,
    HourlyActivityStep,
    StudioSession,
    User,
    UserRole,
)
from app.main import app
from app.services import token_service


@pytest.fixture()
def db_session() -> Session:
    """Create in-memory SQLite database session for testing."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    """Create test client with overridden database dependency."""

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _auth_headers(user: User) -> dict[str, str]:
    """Generate Bearer authorization headers for a given user."""
    token = token_service.issue_access_token(user)
    return {"Authorization": f"Bearer {token}"}


def test_export_unauthenticated(client: TestClient) -> None:
    """Unauthenticated requests must be rejected with 401 Unauthorized."""
    response = client.post("/api/v1/users/me/export")
    assert response.status_code == 401


def test_export_empty_user(client: TestClient, db_session: Session) -> None:
    """Freshly provisioned user with no history receives a valid ZIP with empty summaries."""
    user = User(
        uuid=uuid.uuid4(),
        email="fresh.user@example.com",
        name="Fresh User",
        role=UserRole.user,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    headers = _auth_headers(user)
    response = client.post("/api/v1/users/me/export", headers=headers)

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/zip"
    assert "attachment; filename=" in response.headers["Content-Disposition"]
    assert user.pseudonym in response.headers["Content-Disposition"]

    # Verify ZIP integrity in memory
    zip_bytes = io.BytesIO(response.content)
    with zipfile.ZipFile(zip_bytes, mode="r") as zf:
        namelist = zf.namelist()
        assert "export_metadata.json" in namelist
        assert "profile.json" in namelist
        assert "emergency_contacts.json" in namelist
        assert "devices.json" in namelist
        assert "fall_alerts/summary.json" in namelist
        assert "activity_steps/summary.json" in namelist
        assert "studio_sessions/index.json" in namelist

        # Inactive years/months should generate NO empty files
        assert not any(name.startswith("fall_alerts/alerts_") for name in namelist)
        assert not any(name.startswith("activity_steps/month_") for name in namelist)

        # Verify metadata content
        meta = json.loads(zf.read("export_metadata.json").decode("utf-8"))
        assert meta["user_uuid"] == str(user.uuid)
        assert meta["user_pseudonym"] == user.pseudonym
        assert meta["item_counts"]["emergency_contacts"] == 0
        assert meta["item_counts"]["devices"] == 0
        assert meta["item_counts"]["fall_alerts"] == 0
        assert meta["item_counts"]["active_step_months"] == 0
        assert meta["item_counts"]["studio_sessions"] == 0

        # Verify profile content
        prof = json.loads(zf.read("profile.json").decode("utf-8"))
        assert prof["email"] == "fresh.user@example.com"
        assert prof["name"] == "Fresh User"
        assert prof["role"] == "user"


def test_export_comprehensive_user_data(client: TestClient, db_session: Session) -> None:
    """User with contacts, devices, falls across years, monthly steps, and studio sessions."""
    # 1. Create target user
    user = User(
        uuid=uuid.uuid4(),
        email="patient.active@example.com",
        name="Active Patient",
        role=UserRole.user,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    # 2. Add emergency contact
    contact = AlertContact(
        uuid=uuid.uuid4(),
        user_id=user.id,
        first_name="Alice",
        last_name="Smith",
        email="alice.smith@example.com",
        phone="+33612345678",
        priority=1,
    )
    db_session.add(contact)

    # 3. Add device ownership
    device = Device(
        uuid=uuid.uuid4(),
        device_id="HK-TEST-DEV-01",
        name="Smart Insole Left",
        status=DeviceStatus.online,
        firmware_version="v2.1.0-esp32s3",
    )
    db_session.add(device)
    db_session.commit()

    ownership = DeviceOwnership(
        user_id=user.id,
        device_id=device.device_id,
        bound_at_utc=datetime(2026, 1, 15, 10, 0, tzinfo=UTC),
    )
    db_session.add(ownership)

    # 4. Add fall alerts spanning 2 different years (2025 and 2026)
    fall_2025 = FallAlert(
        uuid=uuid.uuid4(),
        event_id="evt-fall-2025-01",
        user_id=user.id,
        device_id=device.device_id,
        state=FallAlertState.ACKNOWLEDGED.value,
        event_type="fall",
        confidence_score=0.92,
        delivery=FallAlertDelivery.live.value,
        detected_at_utc=datetime(2025, 12, 10, 14, 30, tzinfo=UTC),
        closed_at_utc=datetime(2025, 12, 10, 14, 35, tzinfo=UTC),
    )
    db_session.add(fall_2025)
    db_session.commit()

    notif_2025 = FallAlertNotification(
        alert_id=fall_2025.id,
        rank=1,
        channel="email",
        contact_id=contact.id,
        contact_first_name="Alice",
        contact_last_name="Smith",
        contact_email="alice.smith@example.com",
        status=FallAlertNotificationStatus.ACKNOWLEDGED.value,
        sent_at_utc=datetime(2025, 12, 10, 14, 31, tzinfo=UTC),
        acknowledged_at_utc=datetime(2025, 12, 10, 14, 35, tzinfo=UTC),
    )
    db_session.add(notif_2025)

    fall_2026 = FallAlert(
        uuid=uuid.uuid4(),
        event_id="evt-fall-2026-01",
        user_id=user.id,
        device_id=device.device_id,
        state=FallAlertState.ACKNOWLEDGED.value,
        event_type="fall",
        confidence_score=0.88,
        delivery=FallAlertDelivery.live.value,
        detected_at_utc=datetime(2026, 11, 20, 9, 15, tzinfo=UTC),
    )
    db_session.add(fall_2026)

    # 5. Add steps spanning 2 active months (2026-10 and 2026-11)
    daily_oct = DailyActivityStep(
        user_id=user.id,
        device_id=device.device_id,
        date=date(2026, 10, 25),
        activity_type="walk",
        step_count=5200,
    )
    hourly_oct = HourlyActivityStep(
        user_id=user.id,
        device_id=device.device_id,
        date=date(2026, 10, 25),
        hour=10,
        activity_type="walk",
        step_count=1200,
    )
    daily_nov = DailyActivityStep(
        user_id=user.id,
        device_id=device.device_id,
        date=date(2026, 11, 22),
        activity_type="walk",
        step_count=4300,
    )
    db_session.add_all([daily_oct, hourly_oct, daily_nov])

    # 6. Add studio session
    studio_sess = StudioSession(
        uuid=uuid.uuid4(),
        user_id=user.id,
        author_uuid=user.uuid,
        device_id=device.device_id,
        label="walk",
        sample_count=190,
        duration_sec=10.0,
        is_validated=True,
        is_hitl=False,
    )
    db_session.add(studio_sess)
    db_session.commit()

    # Request export
    headers = _auth_headers(user)
    response = client.post("/api/v1/users/me/export", headers=headers)

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/zip"

    # Inspect in-memory archive
    zip_bytes = io.BytesIO(response.content)
    with zipfile.ZipFile(zip_bytes, mode="r") as zf:
        namelist = zf.namelist()

        # Check root JSON files
        assert "export_metadata.json" in namelist
        assert "profile.json" in namelist
        assert "emergency_contacts.json" in namelist
        assert "devices.json" in namelist

        # Check fall alerts partitioned by year
        assert "fall_alerts/summary.json" in namelist
        assert "fall_alerts/alerts_2025.json" in namelist
        assert "fall_alerts/alerts_2026.json" in namelist
        assert not any("alerts_2024" in name for name in namelist)

        # Check activity steps partitioned by active month
        assert "activity_steps/summary.json" in namelist
        assert "activity_steps/month_2026-10.json" in namelist
        assert "activity_steps/month_2026-11.json" in namelist
        assert not any("month_2026-09" in name for name in namelist)

        # Check studio sessions partitioned per session
        assert "studio_sessions/index.json" in namelist
        assert f"studio_sessions/session_{studio_sess.uuid}.json" in namelist

        # Verify fall alerts content
        alerts_2025 = json.loads(zf.read("fall_alerts/alerts_2025.json").decode("utf-8"))
        assert len(alerts_2025) == 1
        assert alerts_2025[0]["event_id"] == "evt-fall-2025-01"
        assert len(alerts_2025[0]["notifications"]) == 1
        assert alerts_2025[0]["notifications"][0]["contact_email"] == "alice.smith@example.com"

        # Verify steps content
        month_oct = json.loads(zf.read("activity_steps/month_2026-10.json").decode("utf-8"))
        assert month_oct["month"] == "2026-10"
        assert month_oct["total_steps"] == 5200
        assert len(month_oct["daily_records"]) == 1
        assert len(month_oct["daily_records"][0]["hourly_breakdown"]) == 1
        assert month_oct["daily_records"][0]["hourly_breakdown"][0]["step_count"] == 1200

        # Verify studio session detail
        session_json = json.loads(
            zf.read(f"studio_sessions/session_{studio_sess.uuid}.json").decode("utf-8")
        )
        assert session_json["uuid"] == str(studio_sess.uuid)
        assert session_json["label"] == "walk"
        assert session_json["sample_count"] == 190


def test_export_tenant_isolation(client: TestClient, db_session: Session) -> None:
    """User A's export must strictly isolate and contain zero data from User B."""
    # User A
    user_a = User(
        uuid=uuid.uuid4(),
        email="tenant.a@example.com",
        name="Tenant A",
        role=UserRole.user,
        is_active=True,
    )
    # User B
    user_b = User(
        uuid=uuid.uuid4(),
        email="tenant.b@example.com",
        name="Tenant B",
        role=UserRole.user,
        is_active=True,
    )
    db_session.add_all([user_a, user_b])
    db_session.commit()
    db_session.refresh(user_a)
    db_session.refresh(user_b)

    # Seed User B with contacts, devices, falls, steps, and studio sessions
    contact_b = AlertContact(
        uuid=uuid.uuid4(),
        user_id=user_b.id,
        first_name="BobContact",
        last_name="Secret",
        email="secret.bob@private.org",
        priority=1,
    )
    device_b = Device(
        uuid=uuid.uuid4(),
        device_id="HK-DEV-PRIVATE-B",
        name="Private Shoe B",
        status=DeviceStatus.offline,
    )
    db_session.add_all([contact_b, device_b])
    db_session.commit()

    ownership_b = DeviceOwnership(
        user_id=user_b.id,
        device_id=device_b.device_id,
        bound_at_utc=datetime(2026, 2, 1, tzinfo=UTC),
    )
    fall_b = FallAlert(
        uuid=uuid.uuid4(),
        event_id="evt-private-b-999",
        user_id=user_b.id,
        device_id=device_b.device_id,
        state=FallAlertState.PENDING.value,
        event_type="fall",
        confidence_score=0.95,
        delivery=FallAlertDelivery.live.value,
        detected_at_utc=datetime(2026, 3, 1, tzinfo=UTC),
    )
    db_session.add_all([ownership_b, fall_b])
    db_session.commit()

    # User A requests their export
    headers_a = _auth_headers(user_a)
    response = client.post("/api/v1/users/me/export", headers=headers_a)

    assert response.status_code == 200
    zip_bytes = io.BytesIO(response.content)
    with zipfile.ZipFile(zip_bytes, mode="r") as zf:
        full_archive_text = ""
        for name in zf.namelist():
            full_archive_text += zf.read(name).decode("utf-8", errors="ignore") + "\n"

        # Assert no trace of User B's PII or hardware
        assert "tenant.b@example.com" not in full_archive_text
        assert "secret.bob@private.org" not in full_archive_text
        assert "HK-DEV-PRIVATE-B" not in full_archive_text
        assert "evt-private-b-999" not in full_archive_text
        assert str(user_b.uuid) not in full_archive_text
