"""create hourly_activity_steps table

Revision ID: 20261019_0003
Revises: 20261006_0002
Create Date: 2026-10-19 16:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261019_0003"
down_revision: str | Sequence[str] | None = "20261006_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create hourly_activity_steps table with unique constraint and indexes."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "hourly_activity_steps" not in tables:
        op.create_table(
            "hourly_activity_steps",
            sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("device_id", sa.String(length=64), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("hour", sa.SmallInteger(), nullable=False),
            sa.Column("activity_type", sa.String(length=32), nullable=False),
            sa.Column("step_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.UniqueConstraint("device_id", "date", "hour", "activity_type", name="uq_device_date_hour_activity"),
        )
        op.create_index("ix_hourly_activity_steps_user_id", "hourly_activity_steps", ["user_id"])
        op.create_index("ix_hourly_activity_steps_device_id", "hourly_activity_steps", ["device_id"])
        op.create_index("ix_hourly_activity_steps_date", "hourly_activity_steps", ["date"])
        op.create_index("ix_hourly_activity_steps_user_date", "hourly_activity_steps", ["user_id", "date"])
        op.create_index("ix_hourly_activity_steps_device_date", "hourly_activity_steps", ["device_id", "date"])


def downgrade() -> None:
    """Drop hourly_activity_steps table."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "hourly_activity_steps" in tables:
        op.drop_index("ix_hourly_activity_steps_device_date", table_name="hourly_activity_steps")
        op.drop_index("ix_hourly_activity_steps_user_date", table_name="hourly_activity_steps")
        op.drop_index("ix_hourly_activity_steps_date", table_name="hourly_activity_steps")
        op.drop_index("ix_hourly_activity_steps_device_id", table_name="hourly_activity_steps")
        op.drop_index("ix_hourly_activity_steps_user_id", table_name="hourly_activity_steps")
        op.drop_table("hourly_activity_steps")
