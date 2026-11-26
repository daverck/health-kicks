"""Unit tests for the idempotent local development seeder."""

from unittest.mock import MagicMock

from botocore.exceptions import ClientError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import (
    ActivityEvent,
    Base,
    Device,
    DeviceOwnership,
    StudioSession,
    User,
    UserRole,
)
from scripts.seed_dev_data import (
    SEED_DEVICES,
    SEED_STUDIO_SESSIONS,
    SEED_USERS,
    seed_all,
)


def _create_in_memory_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_seed_all_populates_postgres_and_dynamodb_idempotently() -> None:
    """Running seed_all twice must not duplicate users, devices, events, or sessions."""
    db = _create_in_memory_session()
    mock_ddb = MagicMock()
    mock_table = MagicMock()
    mock_batch = MagicMock()
    mock_ddb.Table.return_value = mock_table
    mock_table.batch_writer.return_value.__enter__.return_value = mock_batch

    cfg = Settings(dynamodb_endpoint_url="http://localhost:4566")

    sessions_1, readings_1 = seed_all(db, settings_obj=cfg, dynamodb_resource=mock_ddb)
    sessions_2, readings_2 = seed_all(db, settings_obj=cfg, dynamodb_resource=mock_ddb)

    assert sessions_1 == len(SEED_STUDIO_SESSIONS)
    assert sessions_2 == len(SEED_STUDIO_SESSIONS)
    assert readings_1 == sum(s["sample_count"] for s in SEED_STUDIO_SESSIONS)
    assert readings_2 == readings_1

    assert db.query(User).count() == len(SEED_USERS)
    admin = db.query(User).filter_by(email="serckdavid@gmail.com").one()
    assert admin.role == UserRole.admin

    trainer = db.query(User).filter_by(email="alex.martin@healthkicks.eu").one()
    assert trainer.role == UserRole.trainer

    assert db.query(Device).count() == len(SEED_DEVICES)
    assert db.query(DeviceOwnership).count() == len(SEED_DEVICES)
    assert db.query(ActivityEvent).count() == 5
    assert db.query(StudioSession).count() == len(SEED_STUDIO_SESSIONS)
    assert mock_batch.put_item.call_count == readings_1 + readings_2


def test_seed_all_handles_unavailable_dynamodb_gracefully() -> None:
    """PostgreSQL seeding succeeds even when DynamoDB / LocalStack is unreachable."""
    db = _create_in_memory_session()
    mock_ddb = MagicMock()
    mock_table = MagicMock()
    mock_ddb.Table.return_value = mock_table
    mock_table.batch_writer.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
        "BatchWriteItem",
    )

    cfg = Settings(dynamodb_endpoint_url="http://localhost:4566")
    sessions_count, readings_count = seed_all(db, settings_obj=cfg, dynamodb_resource=mock_ddb)

    assert sessions_count == len(SEED_STUDIO_SESSIONS)
    assert readings_count == 0
    assert db.query(User).count() == len(SEED_USERS)
    assert db.query(StudioSession).count() == len(SEED_STUDIO_SESSIONS)
