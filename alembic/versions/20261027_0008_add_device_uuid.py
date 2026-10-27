"""add uuid column to devices table

Revision ID: 20261027_0008
Revises: 20261026_0007
Create Date: 2026-10-27 09:00:00.000000

"""
from collections.abc import Sequence
import uuid

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261027_0008"
down_revision: str | Sequence[str] | None = "20261026_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add uuid to devices table with backfill, then enforce not null and unique index."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "devices" in tables:
        device_cols = {c["name"] for c in insp.get_columns("devices")}
        device_indices = {i["name"] for i in insp.get_indexes("devices")}

        if "uuid" not in device_cols:
            op.add_column("devices", sa.Column("uuid", sa.UUID(as_uuid=True), nullable=True))

            # Backfill existing devices
            devices = conn.execute(sa.text("SELECT id FROM devices")).fetchall()
            for d in devices:
                did = d[0]
                conn.execute(
                    sa.text("UPDATE devices SET uuid = :u WHERE id = :id"),
                    {"u": str(uuid.uuid4()), "id": did},
                )

            with op.batch_alter_table("devices") as batch_op:
                batch_op.alter_column("uuid", nullable=False)
                if "ix_devices_uuid" not in device_indices:
                    batch_op.create_index("ix_devices_uuid", ["uuid"], unique=True)


def downgrade() -> None:
    """Drop uuid column from devices table."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "devices" in tables:
        device_cols = {c["name"] for c in insp.get_columns("devices")}
        device_indices = {i["name"] for i in insp.get_indexes("devices")}
        if "uuid" in device_cols:
            with op.batch_alter_table("devices") as batch_op:
                if "ix_devices_uuid" in device_indices:
                    batch_op.drop_index("ix_devices_uuid")
                batch_op.drop_column("uuid")
