"""Integration tests for device association, listing, and dissociation endpoints."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import get_db
from app.db.models import Base, Device, DeviceOwnership, DeviceStatus, User, UserRole
from app.main import app
from app.services import auth_service


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
def user_a(db_session) -> User:
    user = User(
        google_sub="sub-user-a",
        email="user_a@example.com",
        name="User A",
        role=UserRole.user,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def user_b(db_session) -> User:
    user = User(
        google_sub="sub-user-b",
        email="user_b@example.com",
        name="User B",
        role=UserRole.user,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def auth_headers_a(user_a) -> dict[str, str]:
    token = auth_service.issue_access_token(user_a)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def auth_headers_b(user_b) -> dict[str, str]:
    token = auth_service.issue_access_token(user_b)
    return {"Authorization": f"Bearer {token}"}


def test_unauthenticated_requests_are_rejected(client) -> None:
    assert client.post("/api/v1/devices", json={"device_id": "d1"}).status_code == 401
    assert client.get("/api/v1/devices").status_code == 401
    assert client.delete("/api/v1/devices/d1").status_code == 401


def test_bind_new_device(client, auth_headers_a, db_session, user_a) -> None:
    payload = {"device_id": "shoe-left-01", "name": "Left Smart Shoe"}
    response = client.post("/api/v1/devices", json=payload, headers=auth_headers_a)
    assert response.status_code == 201

    data = response.json()
    assert data["device_id"] == "shoe-left-01"
    assert data["name"] == "Left Smart Shoe"
    assert data["status"] == DeviceStatus.offline.value
    assert data["last_seen_utc"] is None
    assert data["bound_at_utc"] is not None
    assert "id" in data
    assert "created_at" in data

    # Verify database state
    device = db_session.query(Device).filter_by(device_id="shoe-left-01").one()
    assert device.name == "Left Smart Shoe"
    ownership = (
        db_session.query(DeviceOwnership)
        .filter_by(user_id=user_a.id, device_id="shoe-left-01")
        .one()
    )
    assert ownership is not None


def test_bind_existing_device_updates_name(client, auth_headers_a, db_session) -> None:
    # Pre-create device with an old name
    pre_device = Device(device_id="shoe-right-01", name="Old Name")
    db_session.add(pre_device)
    db_session.commit()

    # User binds and updates name
    payload = {"device_id": "shoe-right-01", "name": "Right Smart Shoe"}
    response = client.post("/api/v1/devices", json=payload, headers=auth_headers_a)
    assert response.status_code == 201
    assert response.json()["name"] == "Right Smart Shoe"

    db_session.refresh(pre_device)
    assert pre_device.name == "Right Smart Shoe"


def test_bind_existing_device_without_name_keeps_name(client, auth_headers_a, db_session) -> None:
    pre_device = Device(device_id="shoe-keep-01", name="Original Name")
    db_session.add(pre_device)
    db_session.commit()

    payload = {"device_id": "shoe-keep-01"}
    response = client.post("/api/v1/devices", json=payload, headers=auth_headers_a)
    assert response.status_code == 201
    assert response.json()["name"] == "Original Name"

    db_session.refresh(pre_device)
    assert pre_device.name == "Original Name"


def test_bind_duplicate_device_raises_400(client, auth_headers_a) -> None:
    payload = {"device_id": "shoe-dup-01", "name": "Smart Shoe"}
    res1 = client.post("/api/v1/devices", json=payload, headers=auth_headers_a)
    assert res1.status_code == 201

    res2 = client.post("/api/v1/devices", json=payload, headers=auth_headers_a)
    assert res2.status_code == 400
    assert res2.json()["detail"] == "Device already bound to this user"


def test_multiple_users_can_bind_same_device(
    client, auth_headers_a, auth_headers_b, user_a, user_b, db_session
) -> None:
    payload = {"device_id": "shared-shoe-01", "name": "Shared Shoe"}
    res_a = client.post("/api/v1/devices", json=payload, headers=auth_headers_a)
    assert res_a.status_code == 201

    res_b = client.post("/api/v1/devices", json=payload, headers=auth_headers_b)
    assert res_b.status_code == 201

    # Check both ownership records exist
    ownerships = db_session.query(DeviceOwnership).filter_by(device_id="shared-shoe-01").all()
    assert len(ownerships) == 2
    user_ids = {o.user_id for o in ownerships}
    assert user_ids == {user_a.id, user_b.id}


def test_list_user_devices_isolation_and_pagination(
    client, auth_headers_a, auth_headers_b
) -> None:
    # User A binds 3 devices
    for i in range(1, 4):
        res = client.post(
            "/api/v1/devices",
            json={"device_id": f"device-a-{i}", "name": f"Device A {i}"},
            headers=auth_headers_a,
        )
        assert res.status_code == 201

    # User B binds 1 device
    res_b = client.post(
        "/api/v1/devices",
        json={"device_id": "device-b-1", "name": "Device B 1"},
        headers=auth_headers_b,
    )
    assert res_b.status_code == 201

    # User A lists devices (all 3)
    list_a = client.get("/api/v1/devices", headers=auth_headers_a).json()
    assert len(list_a) == 3
    assert {d["device_id"] for d in list_a} == {"device-a-1", "device-a-2", "device-a-3"}
    for d in list_a:
        assert "bound_at_utc" in d

    # User B lists devices (only 1)
    list_b = client.get("/api/v1/devices", headers=auth_headers_b).json()
    assert len(list_b) == 1
    assert list_b[0]["device_id"] == "device-b-1"

    # Pagination for User A: limit=2
    paginated_page1 = client.get(
        "/api/v1/devices?skip=0&limit=2", headers=auth_headers_a
    ).json()
    assert len(paginated_page1) == 2

    paginated_page2 = client.get(
        "/api/v1/devices?skip=2&limit=2", headers=auth_headers_a
    ).json()
    assert len(paginated_page2) == 1
    assert paginated_page2[0]["device_id"] not in {d["device_id"] for d in paginated_page1}


def test_unbind_device(client, auth_headers_a, db_session, user_a) -> None:
    # Bind a device
    client.post(
        "/api/v1/devices",
        json={"device_id": "shoe-to-unbind", "name": "To Unbind"},
        headers=auth_headers_a,
    )

    # Delete binding
    del_res = client.delete("/api/v1/devices/shoe-to-unbind", headers=auth_headers_a)
    assert del_res.status_code == 204

    # Device ownership is gone
    ownership = (
        db_session.query(DeviceOwnership)
        .filter_by(user_id=user_a.id, device_id="shoe-to-unbind")
        .one_or_none()
    )
    assert ownership is None

    # Device table row is retained
    device = db_session.query(Device).filter_by(device_id="shoe-to-unbind").one_or_none()
    assert device is not None

    # Listing devices now returns empty
    devices = client.get("/api/v1/devices", headers=auth_headers_a).json()
    assert len(devices) == 0

    # Deleting again returns 404
    del_res2 = client.delete("/api/v1/devices/shoe-to-unbind", headers=auth_headers_a)
    assert del_res2.status_code == 404
    assert del_res2.json()["detail"] == "Device not bound to this user"
