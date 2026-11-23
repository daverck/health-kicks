"""Tests for Immutable Access Audit Logging and Compliance Reporting."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import get_db
from app.db.models import (
    AccessAuditLog,
    Base,
    Device,
    DeviceOwnership,
    DeviceStatus,
    FallAlert,
    FallAlertDelivery,
    FallAlertState,
    User,
    UserRole,
)
from app.main import app
from app.schemas.audit import AuditAction, AuditResource
from app.services import audit_service, token_service


@pytest.fixture()
def db_session() -> Session:
    """Create in-memory SQLite database session and attach immutability triggers."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)

    # Attach SQLite immutability triggers mimicking PostgreSQL trigger behavior
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TRIGGER trg_no_update_access_audit_logs
            BEFORE UPDATE ON access_audit_logs
            BEGIN
                SELECT RAISE(ABORT, 'access_audit_logs table is immutable: updates are strictly prohibited.');
            END;
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TRIGGER trg_no_delete_access_audit_logs
            BEFORE DELETE ON access_audit_logs
            BEGIN
                SELECT RAISE(ABORT, 'access_audit_logs table is immutable: deletions are strictly prohibited.');
            END;
            """
        )

    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    """Create test client with overridden database session."""

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


def test_audit_log_immutability_triggers(db_session: Session) -> None:
    """Audit logs must be strictly append-only: updates and deletes are rejected at the DB level."""
    admin_uuid = uuid.uuid4()
    patient_uuid = uuid.uuid4()

    entry = AccessAuditLog(
        uuid=uuid.uuid4(),
        actor_uuid=admin_uuid,
        actor_role="admin",
        actor_email="admin@healthkicks.org",
        patient_uuid=patient_uuid,
        patient_pseudonym="SUBJ-12345678",
        action="READ",
        resource="profile",
        created_at_utc=datetime.now(UTC),
    )
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)

    # 1. Attempt UPDATE -> must raise exception and abort
    entry.action = "DELETE"
    with pytest.raises(DBAPIError) as exc_info:
        db_session.commit()
    assert "access_audit_logs table is immutable" in str(exc_info.value)
    db_session.rollback()

    # 2. Attempt DELETE -> must raise exception and abort
    db_session.delete(entry)
    with pytest.raises(DBAPIError) as exc_info:
        db_session.commit()
    assert "access_audit_logs table is immutable" in str(exc_info.value)
    db_session.rollback()


def test_audit_log_created_when_admin_updates_patient(
    client: TestClient, db_session: Session
) -> None:
    """Admin updating a patient profile triggers an immutable audit log entry."""
    admin = User(
        uuid=uuid.uuid4(),
        email="compliance.admin@healthkicks.org",
        name="Compliance Admin",
        role=UserRole.admin,
        is_active=True,
    )
    patient = User(
        uuid=uuid.uuid4(),
        email="patient.test@healthkicks.org",
        name="John Patient",
        role=UserRole.user,
        is_active=True,
    )
    db_session.add_all([admin, patient])
    db_session.commit()
    db_session.refresh(admin)
    db_session.refresh(patient)

    headers = _auth_headers(admin)
    response = client.patch(
        f"/api/v1/users/{patient.uuid}",
        headers=headers,
        json={"name": "John Updated", "avatar_url": "https://example.com/pic.png"},
    )
    assert response.status_code == 200

    # Query audit logs
    logs = (
        db_session.query(AccessAuditLog).filter(AccessAuditLog.patient_uuid == patient.uuid).all()
    )
    assert len(logs) == 1
    log = logs[0]
    assert log.actor_uuid == admin.uuid
    assert log.actor_role == "admin"
    assert log.action == "UPDATE"
    assert log.resource == "profile"
    assert log.patient_pseudonym == patient.pseudonym


def test_audit_log_self_access_excluded(client: TestClient, db_session: Session) -> None:
    """A user updating their own profile must NOT generate administrative audit log entries."""
    user = User(
        uuid=uuid.uuid4(),
        email="self.patient@healthkicks.org",
        name="Self Patient",
        role=UserRole.user,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    headers = _auth_headers(user)
    response = client.patch(
        f"/api/v1/users/{user.uuid}",
        headers=headers,
        json={"name": "Self Patient Renamed"},
    )
    assert response.status_code == 200

    # Verify no audit entries created for self-access
    logs = db_session.query(AccessAuditLog).filter(AccessAuditLog.patient_uuid == user.uuid).all()
    assert len(logs) == 0


def test_audit_log_on_admin_reading_patient_fall_alert(
    client: TestClient, db_session: Session
) -> None:
    """Admin inspecting a patient's fall alert detail logs an audit entry."""
    admin = User(
        uuid=uuid.uuid4(),
        email="admin.clinician@healthkicks.org",
        name="Admin Clinician",
        role=UserRole.admin,
        is_active=True,
    )
    patient = User(
        uuid=uuid.uuid4(),
        email="patient.falls@healthkicks.org",
        name="Fall Patient",
        role=UserRole.user,
        is_active=True,
    )
    device = Device(
        uuid=uuid.uuid4(),
        device_id="HK-AUDIT-DEV-01",
        name="Shoe Fall Test",
        status=DeviceStatus.online,
    )
    db_session.add_all([admin, patient, device])
    db_session.commit()
    db_session.refresh(admin)
    db_session.refresh(patient)

    ownership = DeviceOwnership(user_id=patient.id, device_id=device.device_id)
    fall = FallAlert(
        uuid=uuid.uuid4(),
        event_id="evt-audit-fall-01",
        user_id=patient.id,
        device_id=device.device_id,
        state=FallAlertState.ACKNOWLEDGED.value,
        event_type="fall",
        confidence_score=0.95,
        delivery=FallAlertDelivery.live.value,
        detected_at_utc=datetime.now(UTC),
    )
    db_session.add_all([ownership, fall])
    db_session.commit()

    headers = _auth_headers(admin)
    response = client.get(f"/api/v1/fall-alerts/{fall.uuid}", headers=headers)
    assert response.status_code == 200

    # Verify audit entry was logged
    audit_entries = (
        db_session.query(AccessAuditLog)
        .filter(
            AccessAuditLog.patient_uuid == patient.uuid,
            AccessAuditLog.resource == "fall_alerts",
        )
        .all()
    )
    assert len(audit_entries) == 1
    entry = audit_entries[0]
    assert entry.actor_uuid == admin.uuid
    assert entry.action == "READ"
    assert entry.resource_id == str(fall.uuid)


def test_admin_query_audit_logs_endpoint(client: TestClient, db_session: Session) -> None:
    """Admin can query GET /api/v1/audit/access-logs with pagination and filters."""
    admin = User(
        uuid=uuid.uuid4(),
        email="super.auditor@healthkicks.org",
        name="Super Auditor",
        role=UserRole.admin,
        is_active=True,
    )
    patient = User(
        uuid=uuid.uuid4(),
        email="target.patient@healthkicks.org",
        name="Target Patient",
        role=UserRole.user,
        is_active=True,
    )
    db_session.add_all([admin, patient])
    db_session.commit()
    db_session.refresh(admin)
    db_session.refresh(patient)

    # Seed 3 audit entries
    for i in range(3):
        audit_service.record_access_log(
            db=db_session,
            actor=admin,
            patient=patient,
            action=AuditAction.READ,
            resource=AuditResource.profile,
            resource_id=f"rec-{i}",
        )

    headers = _auth_headers(admin)
    response = client.get(
        f"/api/v1/audit/access-logs?patient_uuid={patient.uuid}&resource=profile",
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["items"]) == 3
    assert data["items"][0]["actor_role"] == "admin"
    assert data["items"][0]["resource"] == "profile"


def test_non_admin_cannot_access_audit_logs(client: TestClient, db_session: Session) -> None:
    """Regular users and trainers must receive 403 Forbidden on the audit logs endpoint."""
    trainer = User(
        uuid=uuid.uuid4(),
        email="trainer.user@healthkicks.org",
        name="Trainer User",
        role=UserRole.trainer,
        is_active=True,
    )
    patient = User(
        uuid=uuid.uuid4(),
        email="regular.user@healthkicks.org",
        name="Regular User",
        role=UserRole.user,
        is_active=True,
    )
    db_session.add_all([trainer, patient])
    db_session.commit()
    db_session.refresh(trainer)
    db_session.refresh(patient)

    # Trainer attempt
    headers_trainer = _auth_headers(trainer)
    res_trainer = client.get("/api/v1/audit/access-logs", headers=headers_trainer)
    assert res_trainer.status_code == 403

    # Regular user attempt
    headers_patient = _auth_headers(patient)
    res_patient = client.get("/api/v1/audit/access-logs", headers=headers_patient)
    assert res_patient.status_code == 403
