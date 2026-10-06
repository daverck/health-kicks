"""Add device hardware health metadata (firmware_version and last_calibration_time)

Revision ID: 20261006_0002
Revises: 7f3a8b2c1d9e
Create Date: 2026-10-06 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20261006_0002"
down_revision: Union[str, Sequence[str], None] = "7f3a8b2c1d9e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add firmware_version and last_calibration_time columns to devices table."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    existing_cols = {c["name"] for c in insp.get_columns("devices")}

    if "firmware_version" not in existing_cols:
        op.add_column(
            "devices",
            sa.Column("firmware_version", sa.String(length=64), nullable=True, server_default="v1.2.0-esp32s3"),
        )
    if "last_calibration_time" not in existing_cols:
        op.add_column(
            "devices",
            sa.Column("last_calibration_time", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    """Drop firmware_version and last_calibration_time columns from devices table."""
    op.drop_column("devices", "last_calibration_time")
    op.drop_column("devices", "firmware_version")
