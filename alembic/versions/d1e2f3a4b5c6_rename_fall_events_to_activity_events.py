"""rename fall_events to activity_events and remove unused columns

Revision ID: d1e2f3a4b5c6
Revises: c2f4e81a3d90
Create Date: 2026-09-09 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'c2f4e81a3d90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: rename fall_events to activity_events and drop status_enum and raw_imu_json."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    # 1. Rename table or create activity_events if neither exists
    if "fall_events" in tables and "activity_events" not in tables:
        op.rename_table("fall_events", "activity_events")
    elif "activity_events" not in tables:
        op.create_table(
            "activity_events",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("device_id", sa.String(length=128), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("timestamp_utc", sa.DateTime(timezone=True), nullable=False),
            sa.Column("confidence_score", sa.Float(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_activity_events_device_id"), "activity_events", ["device_id"], unique=False)
        op.create_index(op.f("ix_activity_events_timestamp_utc"), "activity_events", ["timestamp_utc"], unique=False)
        return

    # 2. Inspect columns in activity_events
    insp = sa.inspect(conn)
    cols = {c["name"] for c in insp.get_columns("activity_events")}
    is_sqlite = conn.dialect.name == "sqlite"

    if is_sqlite:
        with op.batch_alter_table("activity_events") as batch_op:
            if "status_enum" in cols:
                batch_op.drop_column("status_enum")
            if "raw_imu_json" in cols:
                batch_op.drop_column("raw_imu_json")
    else:
        if "status_enum" in cols:
            op.drop_column("activity_events", "status_enum")
        if "raw_imu_json" in cols:
            op.drop_column("activity_events", "raw_imu_json")

    # 3. Update indices
    insp = sa.inspect(conn)
    existing_indices = {idx["name"] for idx in insp.get_indexes("activity_events")}

    if "ix_fall_events_device_id" in existing_indices:
        op.drop_index("ix_fall_events_device_id", table_name="activity_events")
    if "ix_activity_events_device_id" not in existing_indices:
        op.create_index(op.f("ix_activity_events_device_id"), "activity_events", ["device_id"], unique=False)

    if "ix_fall_events_timestamp_utc" in existing_indices:
        op.drop_index("ix_fall_events_timestamp_utc", table_name="activity_events")
    if "ix_activity_events_timestamp_utc" not in existing_indices:
        op.create_index(op.f("ix_activity_events_timestamp_utc"), "activity_events", ["timestamp_utc"], unique=False)

    # 4. Drop enum type in PostgreSQL if present
    if conn.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS fallstatus;")


def downgrade() -> None:
    """Downgrade schema: restore status_enum, raw_imu_json, and rename to fall_events."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "activity_events" not in tables:
        return

    is_sqlite = conn.dialect.name == "sqlite"

    # 1. Restore columns
    if conn.dialect.name == "postgresql":
        op.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'fallstatus') THEN CREATE TYPE fallstatus AS ENUM ('detected', 'acknowledged'); END IF; END $$;")
        op.add_column("activity_events", sa.Column("raw_imu_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
        op.add_column("activity_events", sa.Column("status_enum", sa.Enum("detected", "acknowledged", name="fallstatus"), nullable=False, server_default=sa.text("'detected'")))
    elif is_sqlite:
        with op.batch_alter_table("activity_events") as batch_op:
            batch_op.add_column(sa.Column("raw_imu_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
            batch_op.add_column(sa.Column("status_enum", sa.String(), nullable=False, server_default=sa.text("'detected'")))
    else:
        op.add_column("activity_events", sa.Column("raw_imu_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
        op.add_column("activity_events", sa.Column("status_enum", sa.String(), nullable=False, server_default=sa.text("'detected'")))

    # 2. Revert indices
    insp = sa.inspect(conn)
    existing_indices = {idx["name"] for idx in insp.get_indexes("activity_events")}
    if "ix_activity_events_device_id" in existing_indices:
        op.drop_index("ix_activity_events_device_id", table_name="activity_events")
    if "ix_activity_events_timestamp_utc" in existing_indices:
        op.drop_index("ix_activity_events_timestamp_utc", table_name="activity_events")

    # 3. Rename table back
    op.rename_table("activity_events", "fall_events")
    op.create_index(op.f("ix_fall_events_device_id"), "fall_events", ["device_id"], unique=False)
    op.create_index(op.f("ix_fall_events_timestamp_utc"), "fall_events", ["timestamp_utc"], unique=False)
