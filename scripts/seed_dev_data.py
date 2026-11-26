"""Idempotent local development seeder for PostgreSQL and LocalStack DynamoDB.

Usage:
    python -m scripts.seed_dev_data
"""

from __future__ import annotations

import logging
import math
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.core.pseudonymization import get_subject_pseudonym
from app.db.database import build_engine
from app.db.models import (
    ActivityEvent,
    Device,
    DeviceOwnership,
    DeviceStatus,
    StudioSession,
    User,
    UserRole,
)

logger = logging.getLogger("healthkicks.seed")

SEED_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

SEED_USERS: list[dict[str, Any]] = [
    {
        "email": "serckdavid@gmail.com",
        "name": "David Serck",
        "role": UserRole.admin,
    },
    {
        "email": "alex.martin@healthkicks.eu",
        "name": "Alex Martin",
        "role": UserRole.trainer,
    },
]

SEED_DEVICES: list[dict[str, Any]] = [
    {
        "device_id": "hk-device-0001",
        "name": "Clip Lacet #01 (Marie D.)",
        "status": DeviceStatus.online,
        "firmware_version": "v1.4.0-esp32s3",
        "owner_email": "serckdavid@gmail.com",
    },
    {
        "device_id": "hk-device-0002",
        "name": "Clip Lacet #02 (Banc Test Lab)",
        "status": DeviceStatus.offline,
        "firmware_version": "v1.4.0-esp32s3",
        "owner_email": "serckdavid@gmail.com",
    },
    {
        "device_id": "HK-SHOE-001",
        "name": "Module Prototype HK-SHOE-001",
        "status": DeviceStatus.online,
        "firmware_version": "v1.3.2-esp32s3",
        "owner_email": "alex.martin@healthkicks.eu",
    },
]

SEED_STUDIO_SESSIONS: list[dict[str, Any]] = [
    {
        "key": "dev-sess-001",
        "device_id": "hk-device-0001",
        "owner_email": "serckdavid@gmail.com",
        "label": "walk",
        "sample_count": 20,
        "duration_sec": 5.0,
        "is_validated": True,
        "is_hitl": False,
        "hours_ago": 2,
    },
    {
        "key": "dev-sess-002",
        "device_id": "hk-device-0001",
        "owner_email": "serckdavid@gmail.com",
        "label": "fall_forward",
        "sample_count": 20,
        "duration_sec": 4.0,
        "is_validated": False,
        "is_hitl": True,
        "hours_ago": 4,
    },
    {
        "key": "dev-sess-003",
        "device_id": "hk-device-0002",
        "owner_email": "serckdavid@gmail.com",
        "label": "run",
        "sample_count": 20,
        "duration_sec": 10.0,
        "is_validated": True,
        "is_hitl": False,
        "hours_ago": 8,
    },
    {
        "key": "dev-sess-004",
        "device_id": "HK-SHOE-001",
        "owner_email": "alex.martin@healthkicks.eu",
        "label": "stairs_up",
        "sample_count": 20,
        "duration_sec": 7.5,
        "is_validated": True,
        "is_hitl": True,
        "hours_ago": 16,
    },
    {
        "key": "dev-sess-005",
        "device_id": "hk-device-0002",
        "owner_email": "serckdavid@gmail.com",
        "label": "idle",
        "sample_count": 20,
        "duration_sec": 5.0,
        "is_validated": False,
        "is_hitl": False,
        "hours_ago": 24,
    },
    {
        "key": "dev-sess-006",
        "device_id": "hk-device-0001",
        "owner_email": "serckdavid@gmail.com",
        "label": "jump",
        "sample_count": 20,
        "duration_sec": 3.0,
        "is_validated": True,
        "is_hitl": False,
        "hours_ago": 36,
    },
    {
        "key": "dev-sess-007",
        "device_id": "HK-SHOE-001",
        "owner_email": "alex.martin@healthkicks.eu",
        "label": "fall_backward",
        "sample_count": 20,
        "duration_sec": 4.5,
        "is_validated": True,
        "is_hitl": True,
        "hours_ago": 48,
    },
    {
        "key": "dev-sess-008",
        "device_id": "hk-device-0001",
        "owner_email": "serckdavid@gmail.com",
        "label": "squats",
        "sample_count": 20,
        "duration_sec": 12.0,
        "is_validated": False,
        "is_hitl": False,
        "hours_ago": 60,
    },
]


def _seed_users(db: Session) -> dict[str, User]:
    """Upsert default local development users."""
    users_by_email: dict[str, User] = {}
    for spec in SEED_USERS:
        email = spec["email"].lower()
        user = db.query(User).filter_by(email=email).one_or_none()
        if user is None:
            user_uuid = uuid.uuid5(SEED_NAMESPACE, f"user:{email}")
            user = User(
                uuid=user_uuid,
                pseudonym=get_subject_pseudonym(str(user_uuid)),
                email=email,
                name=spec["name"],
                role=spec["role"],
                is_active=True,
            )
            db.add(user)
            db.flush()
            logger.info("Created user %s (role=%s)", email, spec["role"].value)
        else:
            user.role = spec["role"]
            user.is_active = True
            if not user.name:
                user.name = spec["name"]
            logger.info("User %s already exists (role=%s)", email, user.role.value)
        users_by_email[email] = user
    return users_by_email


def _seed_devices(db: Session, users_by_email: dict[str, User]) -> None:
    """Create test devices and bind ownerships idempotently."""
    now = datetime.now(UTC)
    for spec in SEED_DEVICES:
        device = db.query(Device).filter_by(device_id=spec["device_id"]).one_or_none()
        if device is None:
            device = Device(
                uuid=uuid.uuid5(SEED_NAMESPACE, f"device:{spec['device_id']}"),
                device_id=spec["device_id"],
                name=spec["name"],
                status=spec["status"],
                firmware_version=spec["firmware_version"],
                last_seen_utc=now - timedelta(minutes=5)
                if spec["status"] == DeviceStatus.online
                else now - timedelta(hours=12),
                last_calibration_time=now - timedelta(days=1),
            )
            db.add(device)
            db.flush()
            logger.info("Created device %s", spec["device_id"])

        owner = users_by_email[spec["owner_email"]]
        ownership = (
            db.query(DeviceOwnership)
            .filter_by(user_id=owner.id, device_id=spec["device_id"])
            .one_or_none()
        )
        if ownership is None:
            db.add(DeviceOwnership(user_id=owner.id, device_id=spec["device_id"]))
            logger.info("Bound device %s to %s", spec["device_id"], owner.email)


def _seed_activity_events(db: Session) -> None:
    """Insert sample activity events if none exist for the primary test device."""
    existing = db.query(ActivityEvent).filter_by(device_id="hk-device-0001").first()
    if existing is not None:
        logger.info("Activity events already seeded for hk-device-0001")
        return

    now = datetime.now(UTC)
    sample_events = [
        ("hk-device-0001", "walk", 0.96, 1),
        ("hk-device-0001", "stairs_up", 0.91, 2),
        ("hk-device-0001", "run", 0.94, 4),
        ("hk-device-0001", "idle", 0.99, 6),
        ("HK-SHOE-001", "walk", 0.92, 3),
    ]
    for device_id, event_type, confidence, hours_ago in sample_events:
        db.add(
            ActivityEvent(
                device_id=device_id,
                event_type=event_type,
                confidence_score=confidence,
                timestamp_utc=now - timedelta(hours=hours_ago),
            )
        )
    logger.info("Seeded %d activity events", len(sample_events))


def _seed_studio_sessions(db: Session, users_by_email: dict[str, User]) -> list[StudioSession]:
    """Create sample studio recording sessions idempotently."""
    now = datetime.now(UTC)
    seeded: list[StudioSession] = []
    for spec in SEED_STUDIO_SESSIONS:
        sess_uuid = uuid.uuid5(SEED_NAMESPACE, spec["key"])
        session_obj = db.get(StudioSession, sess_uuid)
        if session_obj is None:
            owner = users_by_email[spec["owner_email"]]
            session_obj = StudioSession(
                uuid=sess_uuid,
                user_id=owner.id,
                author_uuid=owner.uuid,
                device_id=spec["device_id"],
                label=spec["label"],
                sample_count=spec["sample_count"],
                duration_sec=spec["duration_sec"],
                is_validated=spec["is_validated"],
                is_hitl=spec["is_hitl"],
                created_at=now - timedelta(hours=spec["hours_ago"]),
            )
            db.add(session_obj)
            logger.info("Created studio session %s (%s)", sess_uuid, spec["label"])
        seeded.append(session_obj)
    return seeded


def _seed_dynamodb_telemetry(
    sessions: list[StudioSession],
    settings_obj: Settings = settings,
    dynamodb_resource: Any | None = None,
) -> int:
    """Write synthetic IMU readings to LocalStack DynamoDB for each studio session."""
    try:
        if dynamodb_resource is None:
            kwargs: dict[str, Any] = {"region_name": settings_obj.aws_region}
            if settings_obj.dynamodb_endpoint_url:
                kwargs["endpoint_url"] = settings_obj.dynamodb_endpoint_url
            dynamodb_resource = boto3.resource("dynamodb", **kwargs)

        table = dynamodb_resource.Table(settings_obj.dynamodb_telemetry_table)
        written = 0
        with table.batch_writer() as batch:
            for sess in sessions:
                base_ts_us = int(sess.created_at.timestamp() * 1_000_000)
                sample_count = max(1, sess.sample_count)
                for idx in range(sample_count):
                    t = idx * 0.2
                    batch.put_item(
                        Item={
                            "device_id": sess.device_id,
                            "timestamp": base_ts_us + idx * 50_000,
                            "session_id": str(sess.uuid),
                            "label": sess.label,
                            "ax": Decimal(str(round(math.sin(t) * 0.85 + 0.1, 4))),
                            "ay": Decimal(str(round(math.cos(t * 1.2) * 0.95 - 0.05, 4))),
                            "az": Decimal(str(round(math.sin(t * 0.5) * 0.35 + 0.98, 4))),
                            "gx": Decimal(str(round(math.cos(t * 0.8) * 45.2, 2))),
                            "gy": Decimal(str(round(math.sin(t * 1.1) * 32.8, 2))),
                            "gz": Decimal(str(round(math.cos(t * 1.4) * 18.5, 2))),
                        }
                    )
                    written += 1
        logger.info(
            "Seeded %d IMU readings into DynamoDB table '%s'",
            written,
            settings_obj.dynamodb_telemetry_table,
        )
        return written
    except (BotoCoreError, ClientError, OSError) as err:
        logger.warning(
            "Skipping DynamoDB telemetry seeding (table '%s' unavailable: %s)",
            settings_obj.dynamodb_telemetry_table,
            err,
        )
        return 0


def seed_all(
    db: Session,
    settings_obj: Settings = settings,
    dynamodb_resource: Any | None = None,
) -> tuple[int, int]:
    """Run PostgreSQL and DynamoDB seeding idempotently.

    Returns:
        Tuple of (studio_sessions_count, dynamodb_readings_written).
    """
    users_by_email = _seed_users(db)
    _seed_devices(db, users_by_email)
    _seed_activity_events(db)
    sessions = _seed_studio_sessions(db, users_by_email)
    db.commit()

    readings_written = _seed_dynamodb_telemetry(
        sessions,
        settings_obj=settings_obj,
        dynamodb_resource=dynamodb_resource,
    )
    return len(sessions), readings_written


def main() -> int:
    """CLI entrypoint for local dev seeding."""
    logging.basicConfig(level=logging.INFO, format="[seed] %(levelname)s: %(message)s")
    engine = build_engine(settings)
    with Session(engine) as db:
        session_count, readings_count = seed_all(db, settings_obj=settings)
    logger.info(
        "Seeding finished (%d studio sessions in PostgreSQL, %d telemetry frames in DynamoDB).",
        session_count,
        readings_count,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
