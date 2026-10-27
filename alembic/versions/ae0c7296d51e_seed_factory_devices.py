"""seed_factory_devices

Revision ID: ae0c7296d51e
Revises: ac994f8fce7b
Create Date: 2026-09-03 11:28:00.377094

"""
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'ae0c7296d51e'
down_revision: str | Sequence[str] | None = 'ac994f8fce7b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

FACTORY_DEVICE_IDS = ["HK-1", "HK-2"]  # Keep strictly HK-1 and HK-2


def upgrade() -> None:
    """Seed initial factory devices into devices table idempotently."""
    conn = op.get_bind()
    is_postgres = conn.dialect.name == "postgresql"

    # In PostgreSQL, the devicestatus enum type was created in migration ac994f8fce7b.
    # We declare the enum with create_type=False and cast the literal to avoid psycopg2 datatype mismatch.
    if is_postgres:
        device_status_type = sa.Enum("online", "offline", name="devicestatus", create_type=False)
        status_val = sa.cast(sa.literal("offline"), device_status_type)
    else:
        device_status_type = sa.String()
        status_val = "offline"

    device_cols = {c["name"] for c in sa.inspect(conn).get_columns("devices")}
    has_uuid = "uuid" in device_cols

    columns = [
        sa.column("device_id", sa.String),
        sa.column("name", sa.String),
        sa.column("status", device_status_type),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("last_seen_utc", sa.DateTime(timezone=True)),
    ]
    if has_uuid:
        columns.append(sa.column("uuid", sa.UUID(as_uuid=True)))
    devices_table = sa.table("devices", *columns)

    now = datetime.now(UTC)
    inserted_count = 0
    already_existing_count = 0

    for device_id in FACTORY_DEVICE_IDS:
        exists = conn.execute(
            sa.text("SELECT 1 FROM devices WHERE device_id = :did"),
            {"did": device_id},
        ).scalar()
        if not exists:
            row_vals = {
                "device_id": device_id,
                "name": f"HealthKicks Shoe {device_id.split('-')[-1]}",
                "status": status_val,
                "created_at": now,
                "last_seen_utc": None,
            }
            if has_uuid:
                row_vals["uuid"] = uuid.uuid4()
            conn.execute(devices_table.insert().values(**row_vals))
            inserted_count += 1
        else:
            already_existing_count += 1

    msg = (
        f"[alembic] Factory devices seed completed: {inserted_count} inserted, "
        f"{already_existing_count} already existed in database."
    )
    print(msg)
    logger.info(msg)


def downgrade() -> None:
    """Remove seeded factory devices."""
    conn = op.get_bind()
    deleted_count = 0
    for device_id in FACTORY_DEVICE_IDS:
        res = conn.execute(
            sa.text("DELETE FROM devices WHERE device_id = :did"),
            {"did": device_id},
        )
        if res.rowcount:
            deleted_count += res.rowcount

    msg = f"[alembic] Factory devices downgrade completed: {deleted_count} removed."
    print(msg)
    logger.info(msg)
