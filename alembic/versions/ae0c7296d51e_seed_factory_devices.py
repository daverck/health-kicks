"""seed_factory_devices

Revision ID: ae0c7296d51e
Revises: ac994f8fce7b
Create Date: 2026-09-03 11:28:00.377094

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ae0c7296d51e'
down_revision: Union[str, Sequence[str], None] = 'ac994f8fce7b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FACTORY_DEVICE_IDS = [f"HK-SHOE-{i:03d}" for i in range(1, 11)]


def upgrade() -> None:
    """Seed initial factory devices into devices table idempotently."""
    conn = op.get_bind()
    devices_table = sa.table(
        "devices",
        sa.column("device_id", sa.String),
        sa.column("name", sa.String),
        sa.column("status", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("last_seen_utc", sa.DateTime(timezone=True)),
    )

    now = datetime.now(timezone.utc)
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
                    status="offline",
                    created_at=now,
                    last_seen_utc=None,
                )
            )


def downgrade() -> None:
    """Remove seeded factory devices."""
    conn = op.get_bind()
    for device_id in FACTORY_DEVICE_IDS:
        conn.execute(
            sa.text("DELETE FROM devices WHERE device_id = :did"),
            {"did": device_id},
        )
