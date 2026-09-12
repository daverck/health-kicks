"""Tests for ActivityEvent model, database schema, and activity events API endpoints."""

from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.ingestion import settings as ingestion_settings
from app.db.database import get_db
from app.db.models import ActivityEvent, Base, Device, User, UserRole
from app.main import app
from app.services import token_service


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
def client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def test_user(db_session) -> User:
    user = User(
        google_sub="sub-activity-user",
        email="activity_user@example.com",
        name="Activity Tester",
        role=UserRole.user,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def auth_headers(test_user) -> dict[str, str]:
    token = token_service.issue_access_token(test_user)
    return {"Authorization": f"Bearer {token}"}


def test_activity_event_model_structure(db_session):
    """Verify ActivityEvent table and columns match the new simplified specification."""
    mapper = inspect(ActivityEvent)
    column_names = {col.key for col in mapper.columns}

    # Verify expected columns
    assert column_names == {"id", "device_id", "event_type", "timestamp_utc", "confidence_score"}

    # Verify dropped/removed legacy columns do NOT exist
    assert "status_enum" not in column_names
    assert "raw_imu_json" not in column_names
    assert not hasattr(ActivityEvent, "status_enum")
    assert not hasattr(ActivityEvent, "raw_imu_json")

    # Verify table name
    assert ActivityEvent.__tablename__ == "activity_events"


def test_list_activities_endpoint_unauthorized(client):
    """GET /devices/{device_id}/events/activities requires authentication."""
    response = client.get("/api/v1/devices/HK-1/events/activities")
    assert response.status_code == 401


def test_list_falls_endpoint_is_removed(client, auth_headers):
    """Verify that legacy /devices/{device_id}/events/falls route is removed (no retrocompatibility bloat)."""
    response = client.get("/api/v1/devices/HK-1/events/falls", headers=auth_headers)
    assert response.status_code == 404


def test_list_activities_pagination_and_sorting(client, db_session, auth_headers):
    """Verify listing activities supports pagination and orders by timestamp_utc descending."""
    now = datetime.now(timezone.utc)

    # Seed 5 activities for HK-1 and 1 for HK-2
    events_hk1 = [
        ActivityEvent(
            device_id="HK-1",
            event_type="walk",
            timestamp_utc=now - timedelta(minutes=10),
            confidence_score=0.91,
        ),
        ActivityEvent(
            device_id="HK-1",
            event_type="idle",
            timestamp_utc=now - timedelta(minutes=5),
            confidence_score=0.98,
        ),
        ActivityEvent(
            device_id="HK-1",
            event_type="fall_forward",
            timestamp_utc=now - timedelta(minutes=1),
            confidence_score=0.85,
        ),
        ActivityEvent(
            device_id="HK-1",
            event_type="run",
            timestamp_utc=now - timedelta(minutes=20),
            confidence_score=0.94,
        ),
        ActivityEvent(
            device_id="HK-1",
            event_type="stairs_up",
            timestamp_utc=now - timedelta(minutes=15),
            confidence_score=0.77,
        ),
    ]
    event_hk2 = ActivityEvent(
        device_id="HK-2",
        event_type="walk",
        timestamp_utc=now,
        confidence_score=0.99,
    )

    db_session.add_all(events_hk1 + [event_hk2])
    db_session.commit()

    # Query page 1 with page_size=2
    response = client.get(
        "/api/v1/devices/HK-1/events/activities?page=1&page_size=2",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()

    assert data["total"] == 5
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["items"]) == 2

    # Most recent first: fall_forward (-1 min), then idle (-5 min)
    assert data["items"][0]["event_type"] == "fall_forward"
    assert data["items"][0]["confidence_score"] == 0.85
    assert data["items"][0]["device_id"] == "HK-1"
    # Verify legacy keys are NOT present in response schema
    assert "status_enum" not in data["items"][0]
    assert "raw_imu_json" not in data["items"][0]

    assert data["items"][1]["event_type"] == "idle"
    assert data["items"][1]["confidence_score"] == 0.98

    # Query page 2
    response2 = client.get(
        "/api/v1/devices/HK-1/events/activities?page=2&page_size=2",
        headers=auth_headers,
    )
    assert response2.status_code == 200
    data2 = response2.json()
    assert len(data2["items"]) == 2
    assert data2["items"][0]["event_type"] == "walk"  # -10 min
    assert data2["items"][1]["event_type"] == "stairs_up"  # -15 min

    # Query device with no events
    response_empty = client.get(
        "/api/v1/devices/HK-99/events/activities",
        headers=auth_headers,
    )
    assert response_empty.status_code == 200
    data_empty = response_empty.json()
    assert data_empty["total"] == 0
    assert data_empty["items"] == []


def test_ingest_event_persists_to_activity_events(client, db_session, monkeypatch):
    """Verify ingestion webhook inserts into activity_events table correctly without raw_imu_json."""
    monkeypatch.setattr(
        "app.api.v1.ingestion.settings",
        ingestion_settings.__class__(ingest_token="secret-test-token", environment="production"),
    )

    ingest_payload = {
        "header": {
            "device_id": "HK-3",
            "msg_id": "msg-act-101",
            "timestamp": "2026-03-30T12:00:00Z",
        },
        "payload": {
            "event_type": "idle",
            "confidence_score": 0.96,
            "raw_imu_snapshot": {"ax": 0.01, "ay": 0.02, "az": 9.81},
        },
    }

    headers = {"X-HealthKicks-Ingest-Token": "secret-test-token"}
    res = client.post("/api/v1/ingest/event", json=ingest_payload, headers=headers)
    assert res.status_code == 200
    assert res.json()["duplicate"] is False

    # Check database persistence
    event = db_session.query(ActivityEvent).filter_by(device_id="HK-3").one()
    assert event.event_type == "idle"
    assert event.confidence_score == 0.96
    assert event.timestamp_utc.year == 2026
    assert not hasattr(event, "raw_imu_json")
    assert not hasattr(event, "status_enum")


def test_list_activities_filter_by_specific_event_type(client, db_session, auth_headers):
    """Verify event_type='walk' strictly filters only walk activities."""
    base_time = datetime(2026, 9, 10, 10, 0, 0, tzinfo=timezone.utc)
    for i, event_type in enumerate(["walk", "run", "walk", "fall_forward", "idle"]):
        db_session.add(
            ActivityEvent(
                device_id="HK-FILTER",
                event_type=event_type,
                timestamp_utc=base_time + timedelta(minutes=i),
                confidence_score=0.9,
            )
        )
    db_session.commit()

    resp = client.get(
        "/api/v1/devices/HK-FILTER/events/activities?event_type=walk",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert all(item["event_type"] == "walk" for item in data["items"])


def test_list_activities_filter_by_falls_group(client, db_session, auth_headers):
    """Verify event_type='falls' filters all fall events (fall_forward, fall_lateral, etc.)."""
    base_time = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
    for i, event_type in enumerate(["walk", "fall_forward", "fall_lateral", "fall_backward", "idle"]):
        db_session.add(
            ActivityEvent(
                device_id="HK-FALLS",
                event_type=event_type,
                timestamp_utc=base_time + timedelta(minutes=i),
                confidence_score=0.88,
            )
        )
    db_session.commit()

    resp = client.get(
        "/api/v1/devices/HK-FALLS/events/activities?event_type=falls",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert len(data["items"]) == 3
    assert {item["event_type"] for item in data["items"]} == {
        "fall_forward",
        "fall_lateral",
        "fall_backward",
    }


def test_list_activities_filter_all_or_empty_returns_everything(client, db_session, auth_headers):
    """Verify event_type='all' or absent/empty applies no filtering."""
    base_time = datetime(2026, 9, 10, 14, 0, 0, tzinfo=timezone.utc)
    for i, event_type in enumerate(["walk", "run", "fall_forward"]):
        db_session.add(
            ActivityEvent(
                device_id="HK-ALL",
                event_type=event_type,
                timestamp_utc=base_time + timedelta(minutes=i),
                confidence_score=0.9,
            )
        )
    db_session.commit()

    resp_all = client.get(
        "/api/v1/devices/HK-ALL/events/activities?event_type=all",
        headers=auth_headers,
    )
    assert resp_all.status_code == 200
    assert resp_all.json()["total"] == 3

    resp_empty = client.get(
        "/api/v1/devices/HK-ALL/events/activities?event_type=",
        headers=auth_headers,
    )
    assert resp_empty.status_code == 200
    assert resp_empty.json()["total"] == 3


def test_list_activities_filter_by_date_range(client, db_session, auth_headers):
    """Verify start_date and end_date filtering with both date-only and full timestamps."""
    day1 = datetime(2026, 9, 11, 10, 0, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 9, 12, 15, 0, 0, tzinfo=timezone.utc)
    day3 = datetime(2026, 9, 13, 8, 0, 0, tzinfo=timezone.utc)

    for i, ts in enumerate([day1, day2, day3]):
        db_session.add(
            ActivityEvent(
                device_id="HK-DATES",
                event_type="walk",
                timestamp_utc=ts,
                confidence_score=0.9,
            )
        )
    db_session.commit()

    # Date-only end_date includes full day up to 23:59:59
    resp_date_only = client.get(
        "/api/v1/devices/HK-DATES/events/activities?start_date=2026-09-12&end_date=2026-09-12",
        headers=auth_headers,
    )
    assert resp_date_only.status_code == 200
    data_date_only = resp_date_only.json()
    assert data_date_only["total"] == 1
    assert data_date_only["items"][0]["timestamp_utc"].startswith("2026-09-12")

    # Timestamp range
    resp_range = client.get(
        "/api/v1/devices/HK-DATES/events/activities?start_date=2026-09-11T00:00:00Z&end_date=2026-09-12T23:59:59Z",
        headers=auth_headers,
    )
    assert resp_range.status_code == 200
    assert resp_range.json()["total"] == 2


def test_list_activities_combined_filters_and_total_pagination(client, db_session, auth_headers):
    """Verify combining event_type and date range, ensuring total reflects filters under pagination."""
    base_time = datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)

    # 4 falls on Sept 12, 2 walks on Sept 12, 2 falls on Sept 13
    for i in range(4):
        db_session.add(
            ActivityEvent(
                device_id="HK-COMBO",
                event_type="fall_forward",
                timestamp_utc=base_time + timedelta(hours=i),
                confidence_score=0.9,
            )
        )
    for i in range(2):
        db_session.add(
            ActivityEvent(
                device_id="HK-COMBO",
                event_type="walk",
                timestamp_utc=base_time + timedelta(hours=i),
                confidence_score=0.9,
            )
        )
    for i in range(2):
        db_session.add(
            ActivityEvent(
                device_id="HK-COMBO",
                event_type="fall_lateral",
                timestamp_utc=base_time + timedelta(days=1, hours=i),
                confidence_score=0.9,
            )
        )
    db_session.commit()

    # Filter: falls only on Sept 12 with page_size=2
    resp = client.get(
        "/api/v1/devices/HK-COMBO/events/activities?event_type=falls&start_date=2026-09-12&end_date=2026-09-12&page=1&page_size=2",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 4  # Total matching events, NOT total in table (8)
    assert len(data["items"]) == 2
    assert data["page"] == 1
    assert data["page_size"] == 2


def test_list_activities_start_date_after_end_date_400(client, auth_headers):
    """Verify HTTP 400 error when start_date > end_date."""
    resp = client.get(
        "/api/v1/devices/HK-1/events/activities?start_date=2026-09-15&end_date=2026-09-10",
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "start_date must be before or equal to end_date"
