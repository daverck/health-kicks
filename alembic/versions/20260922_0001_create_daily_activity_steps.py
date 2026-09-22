"""create daily_activity_steps table

Revision ID: 7f3a8b2c1d9e
Revises: f4a5b6c7d8e9
Create Date: 2026-09-22 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7f3a8b2c1d9e"
down_revision: Union[str, Sequence[str], None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create daily_activity_steps table with unique constraint and indexes."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "daily_activity_steps" not in tables:
        op.create_table(
            "daily_activity_steps",
            sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("device_id", sa.String(length=64), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("activity_type", sa.String(length=32), nullable=False),
            sa.Column("step_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.UniqueConstraint("device_id", "date", "activity_type", name="uq_device_date_activity"),
        )
        op.create_index("ix_daily_activity_steps_user_id", "daily_activity_steps", ["user_id"])
        op.create_index("ix_daily_activity_steps_device_id", "daily_activity_steps", ["device_id"])
        op.create_index("ix_daily_activity_steps_date", "daily_activity_steps", ["date"])
        op.create_index("ix_daily_activity_steps_user_date", "daily_activity_steps", ["user_id", "date"])
        op.create_index("ix_daily_activity_steps_device_date", "daily_activity_steps", ["device_id", "date"])


def downgrade() -> None:
    """Drop daily_activity_steps table."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "daily_activity_steps" in tables:
        op.drop_index("ix_daily_activity_steps_device_date", table_name="daily_activity_steps")
        op.drop_index("ix_daily_activity_steps_user_date", table_name="daily_activity_steps")
        op.drop_index("ix_daily_activity_steps_date", table_name="daily_activity_steps")
        op.drop_index("ix_daily_activity_steps_device_id", table_name="daily_activity_steps")
        op.drop_index("ix_daily_activity_steps_user_id", table_name="daily_activity_steps")
        op.drop_table("daily_activity_steps")

