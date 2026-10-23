"""create alert_contacts, fall_alerts, and fall_alert_notifications tables

Revision ID: 20261023_0004
Revises: 20261019_0003
Create Date: 2026-10-23 11:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261023_0004"
down_revision: str | Sequence[str] | None = "20261019_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create alert_contacts, fall_alerts, and fall_alert_notifications tables."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "alert_contacts" not in tables:
        op.create_table(
            "alert_contacts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("first_name", sa.String(length=100), nullable=False),
            sa.Column("last_name", sa.String(length=100), nullable=False),
            sa.Column("email", sa.String(length=320), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False),
            sa.Column("last_test_email_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.UniqueConstraint("user_id", "email", name="uq_alert_contacts_user_email"),
        )
        op.create_index("ix_alert_contacts_user_id", "alert_contacts", ["user_id"])
        op.create_index("ix_alert_contacts_user_priority", "alert_contacts", ["user_id", "priority"])

    if "fall_alerts" not in tables:
        op.create_table(
            "fall_alerts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column("event_id", sa.String(length=64), nullable=False),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("device_id", sa.String(length=128), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("confidence_score", sa.Float(), nullable=False),
            sa.Column("delivery", sa.String(length=16), nullable=False, server_default="live"),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("cancel_reason", sa.String(length=32), nullable=True),
            sa.Column("lat", sa.Float(), nullable=True),
            sa.Column("lon", sa.Float(), nullable=True),
            sa.Column("accuracy_m", sa.Float(), nullable=True),
            sa.Column("location_captured_at_utc", sa.DateTime(timezone=True), nullable=True),
            sa.Column("current_contact_rank", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("contacts_snapshot_json", sa.Text(), nullable=True),
            sa.Column("acknowledged_by_first_name", sa.String(length=100), nullable=True),
            sa.Column("acknowledged_by_last_name", sa.String(length=100), nullable=True),
            sa.Column("acknowledged_by_contact_id", sa.Integer(), nullable=True),
            sa.Column("detected_at_utc", sa.DateTime(timezone=True), nullable=False),
            sa.Column("escalation_started_at_utc", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_action_at_utc", sa.DateTime(timezone=True), nullable=True),
            sa.Column("closed_at_utc", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at_utc",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at_utc",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )
        op.create_index("ix_fall_alerts_event_id", "fall_alerts", ["event_id"], unique=True)
        op.create_index("ix_fall_alerts_user_id", "fall_alerts", ["user_id"])
        op.create_index("ix_fall_alerts_device_id", "fall_alerts", ["device_id"])
        op.create_index("ix_fall_alerts_state", "fall_alerts", ["state"])
        op.create_index("ix_fall_alerts_detected_at_utc", "fall_alerts", ["detected_at_utc"])
        op.create_index("ix_fall_alerts_next_action_at_utc", "fall_alerts", ["next_action_at_utc"])
        op.create_index("ix_fall_alerts_state_next_action", "fall_alerts", ["state", "next_action_at_utc"])
        op.create_index("ix_fall_alerts_user_detected", "fall_alerts", ["user_id", "detected_at_utc"])

    if "fall_alert_notifications" not in tables:
        op.create_table(
            "fall_alert_notifications",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
            sa.Column(
                "alert_id",
                sa.Integer(),
                sa.ForeignKey("fall_alerts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("rank", sa.Integer(), nullable=False),
            sa.Column("contact_id", sa.Integer(), nullable=True),
            sa.Column("contact_first_name", sa.String(length=100), nullable=False),
            sa.Column("contact_last_name", sa.String(length=100), nullable=False),
            sa.Column("contact_email", sa.String(length=320), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column(
                "sent_at_utc",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column("acknowledged_at_utc", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error_message", sa.String(length=512), nullable=True),
        )
        op.create_index("ix_fall_alert_notifications_alert_id", "fall_alert_notifications", ["alert_id"])


def downgrade() -> None:
    """Drop fall alerting tables."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "fall_alert_notifications" in tables:
        op.drop_index("ix_fall_alert_notifications_alert_id", table_name="fall_alert_notifications")
        op.drop_table("fall_alert_notifications")

    if "fall_alerts" in tables:
        op.drop_index("ix_fall_alerts_user_detected", table_name="fall_alerts")
        op.drop_index("ix_fall_alerts_state_next_action", table_name="fall_alerts")
        op.drop_index("ix_fall_alerts_next_action_at_utc", table_name="fall_alerts")
        op.drop_index("ix_fall_alerts_detected_at_utc", table_name="fall_alerts")
        op.drop_index("ix_fall_alerts_state", table_name="fall_alerts")
        op.drop_index("ix_fall_alerts_device_id", table_name="fall_alerts")
        op.drop_index("ix_fall_alerts_user_id", table_name="fall_alerts")
        op.drop_index("ix_fall_alerts_event_id", table_name="fall_alerts")
        op.drop_table("fall_alerts")

    if "alert_contacts" in tables:
        op.drop_index("ix_alert_contacts_user_priority", table_name="alert_contacts")
        op.drop_index("ix_alert_contacts_user_id", table_name="alert_contacts")
        op.drop_table("alert_contacts")
