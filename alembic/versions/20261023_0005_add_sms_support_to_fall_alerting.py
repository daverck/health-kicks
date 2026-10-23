"""add phone and sms channel columns to fall alerting tables

Revision ID: 20261023_0005
Revises: 20261023_0004
Create Date: 2026-10-23 15:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261023_0005"
down_revision: str | Sequence[str] | None = "20261023_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add phone to alert_contacts and channel/contact_phone to fall_alert_notifications."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "alert_contacts" in tables:
        ac_cols = {c["name"] for c in insp.get_columns("alert_contacts")}
        if "phone" not in ac_cols:
            op.add_column(
                "alert_contacts",
                sa.Column("phone", sa.String(length=32), nullable=True),
            )

    if "fall_alert_notifications" in tables:
        fan_cols = {c["name"] for c in insp.get_columns("fall_alert_notifications")}
        if "channel" not in fan_cols:
            op.add_column(
                "fall_alert_notifications",
                sa.Column(
                    "channel",
                    sa.String(length=16),
                    nullable=False,
                    server_default="email",
                ),
            )
        if "contact_phone" not in fan_cols:
            op.add_column(
                "fall_alert_notifications",
                sa.Column("contact_phone", sa.String(length=32), nullable=True),
            )


def downgrade() -> None:
    """Remove phone, channel, and contact_phone columns from fall alerting tables."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "fall_alert_notifications" in tables:
        fan_cols = {c["name"] for c in insp.get_columns("fall_alert_notifications")}
        with op.batch_alter_table("fall_alert_notifications") as batch_op:
            if "contact_phone" in fan_cols:
                batch_op.drop_column("contact_phone")
            if "channel" in fan_cols:
                batch_op.drop_column("channel")

    if "alert_contacts" in tables:
        ac_cols = {c["name"] for c in insp.get_columns("alert_contacts")}
        if "phone" in ac_cols:
            with op.batch_alter_table("alert_contacts") as batch_op:
                batch_op.drop_column("phone")
