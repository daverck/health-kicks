"""seed_factory_devices

Revision ID: ae0c7296d51e
Revises: ac994f8fce7b
Create Date: 2026-09-03 11:28:00.377094

"""
from datetime import datetime, timezone
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ae0c7296d51e'
down_revision: Union[str, Sequence[str], None] = 'ac994f8fce7b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

FACTORY_DEVICE_IDS = [f"HK-SHOE-{i:03d}" for i in range(1, 11)]


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

    devices_table = sa.table(
        "devices",
        sa.column("device_id", sa.String),
        sa.column("name", sa.String),
        sa.column("status", device_status_type),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("last_seen_utc", sa.DateTime(timezone=True)),
    )

    now = datetime.now(timezone.utc)
    inserted_count = 0
    already_existing_count = 0

    for device_id in FACTORY_DEVICE_IDS:
        exists = conn.execute(
            sa.text("SELECT 1 FROM devices WHERE device_id = :did"),
            {"did": device_id},
        ).scalar()
        if not exists:
            conn.execute(
                devices_table.insert().values(
                    device_id=device_id,
                    name=f"HealthKicks Shoe {device_id.split('-')[-1]}",
                    status=status_val,
                    created_at=now,
                    last_seen_utc=None,
                )
            )
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
