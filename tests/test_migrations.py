"""Tests for Alembic migration execution, baseline auto-stamping, and factory device seeding."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config

from alembic import command
from app.db.models import Base


@pytest.fixture()
def alembic_cfg(tmp_path: Path, monkeypatch) -> Config:
    db_file = tmp_path / "migration_test.db"
    db_url = f"sqlite:///{db_file.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("HEALTHKICKS_DATABASE_URL", db_url)

    cfg_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    cfg = Config(str(cfg_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


TEST_DEVICE_ID = "HK-1"


def test_alembic_upgrade_head_from_scratch(alembic_cfg: Config) -> None:
    db_url = alembic_cfg.get_main_option("sqlalchemy.url")
    engine = sa.create_engine(db_url)

    # 1. Run full migration suite
    command.upgrade(alembic_cfg, "head")

    # 2. Verify all 2 factory devices are seeded
    with engine.connect() as conn:
        devices = conn.execute(
            sa.text("SELECT device_id, name, status FROM devices")
        ).fetchall()
        assert len(devices) == 2
        device_ids = {row[0] for row in devices}
        assert TEST_DEVICE_ID in device_ids
        assert device_ids == {"HK-1", "HK-2"}
        for device_id, name, status in devices:
            assert status == "offline"
            assert name == f"HealthKicks Shoe {device_id.split('-')[-1]}"

    # 3. Test downgrade
    command.downgrade(alembic_cfg, "ac994f8fce7b")
    with engine.connect() as conn:
        count = conn.execute(sa.text("SELECT count(*) FROM devices")).scalar()
        assert count == 0

    engine.dispose()


def test_alembic_upgrade_head_handles_preexisting_create_all_schema(
    alembic_cfg: Config,
) -> None:
    db_url = alembic_cfg.get_main_option("sqlalchemy.url")
    engine = sa.create_engine(db_url)

    # Simulate database created via Base.metadata.create_all() without Alembic tracking
    Base.metadata.create_all(engine)

    with engine.connect() as conn:
        tables = set(sa.inspect(conn).get_table_names())
        assert "devices" in tables
        assert "alembic_version" not in tables
        assert conn.execute(sa.text("SELECT count(*) FROM devices")).scalar() == 0

    # Run upgrade head: should auto-detect missing alembic_version, stamp baseline, and apply seeds
    command.upgrade(alembic_cfg, "head")

    with engine.connect() as conn:
        tables = set(sa.inspect(conn).get_table_names())
        assert "alembic_version" in tables
        count = conn.execute(sa.text("SELECT count(*) FROM devices")).scalar()
        assert count == 2

    # Second run should be a clean no-op
    command.upgrade(alembic_cfg, "head")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM devices")).scalar() == 2

    engine.dispose()


def test_programmatic_migrations_with_shared_connection(alembic_cfg: Config) -> None:
    db_url = alembic_cfg.get_main_option("sqlalchemy.url")
    test_engine = sa.create_engine(db_url)

    # Pass connection in attributes directly, simulating run_migrations()
    with test_engine.begin() as conn:
        alembic_cfg.attributes["connection"] = conn
        command.upgrade(alembic_cfg, "head")

    with test_engine.connect() as conn:
        tables = set(sa.inspect(conn).get_table_names())
        assert "devices" in tables
        assert "alembic_version" in tables
        count = conn.execute(sa.text("SELECT count(*) FROM devices")).scalar()
        assert count == 2

    test_engine.dispose()


def test_composite_query_indexes_created_and_downgraded(alembic_cfg: Config) -> None:
    db_url = alembic_cfg.get_main_option("sqlalchemy.url")
    engine = sa.create_engine(db_url)

    command.upgrade(alembic_cfg, "head")

    with engine.connect() as conn:
        insp = sa.inspect(conn)
        studio_indices = {idx["name"] for idx in insp.get_indexes("studio_sessions")}
        assert "ix_studio_sessions_created_at_uuid" in studio_indices
        assert "ix_studio_sessions_validated_created_at" in studio_indices
        assert "ix_studio_sessions_user_id_created_at" in studio_indices
        assert "ix_studio_sessions_label_created_at" in studio_indices
        assert "ix_studio_sessions_device_id_created_at" in studio_indices

        fall_indices = {idx["name"] for idx in insp.get_indexes("fall_alerts")}
        assert "ix_fall_alerts_detected_at_id" in fall_indices
        assert "ix_fall_alerts_state_detected_at" in fall_indices
        assert "ix_fall_alerts_user_state_id" in fall_indices

        notif_indices = {idx["name"] for idx in insp.get_indexes("fall_alert_notifications")}
        assert "ix_fall_alert_notifications_alert_rank" in notif_indices

    command.downgrade(alembic_cfg, "20261027_0009")

    with engine.connect() as conn:
        insp = sa.inspect(conn)
        studio_indices = {idx["name"] for idx in insp.get_indexes("studio_sessions")}
        assert "ix_studio_sessions_created_at_uuid" not in studio_indices
        assert "ix_studio_sessions_validated_created_at" not in studio_indices

        fall_indices = {idx["name"] for idx in insp.get_indexes("fall_alerts")}
        assert "ix_fall_alerts_detected_at_id" not in fall_indices

        notif_indices = {idx["name"] for idx in insp.get_indexes("fall_alert_notifications")}
        assert "ix_fall_alert_notifications_alert_rank" not in notif_indices

    engine.dispose()


