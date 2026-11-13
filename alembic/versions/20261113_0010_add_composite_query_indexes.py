"""add composite indexes for studio_sessions and fall_alerts queries

Revision ID: 20261113_0010
Revises: 20261027_0009
Create Date: 2026-11-13 12:40:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261113_0010"
down_revision: str | Sequence[str] | None = "20261027_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STUDIO_SESSION_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_studio_sessions_created_at_uuid", ["created_at", "uuid"]),
    ("ix_studio_sessions_validated_created_at", ["is_validated", "created_at"]),
    ("ix_studio_sessions_user_id_created_at", ["user_id", "created_at"]),
    ("ix_studio_sessions_label_created_at", ["label", "created_at"]),
    ("ix_studio_sessions_device_id_created_at", ["device_id", "created_at"]),
)

FALL_ALERT_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_fall_alerts_detected_at_id", ["detected_at_utc", "id"]),
    ("ix_fall_alerts_state_detected_at", ["state", "detected_at_utc"]),
    ("ix_fall_alerts_user_state_id", ["user_id", "state", "id"]),
)

FALL_ALERT_NOTIFICATION_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_fall_alert_notifications_alert_rank", ["alert_id", "rank", "id"]),
)


def upgrade() -> None:
    """Create composite indexes on studio_sessions, fall_alerts, and fall_alert_notifications."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "studio_sessions" in tables:
        existing = {idx["name"] for idx in insp.get_indexes("studio_sessions")}
        cols = {c["name"] for c in insp.get_columns("studio_sessions")}
        for idx_name, idx_cols in STUDIO_SESSION_INDEXES:
            if idx_name not in existing and all(c in cols for c in idx_cols):
                op.create_index(idx_name, "studio_sessions", idx_cols, unique=False)

    if "fall_alerts" in tables:
        existing = {idx["name"] for idx in insp.get_indexes("fall_alerts")}
        cols = {c["name"] for c in insp.get_columns("fall_alerts")}
        for idx_name, idx_cols in FALL_ALERT_INDEXES:
            if idx_name not in existing and all(c in cols for c in idx_cols):
                op.create_index(idx_name, "fall_alerts", idx_cols, unique=False)

    if "fall_alert_notifications" in tables:
        existing = {idx["name"] for idx in insp.get_indexes("fall_alert_notifications")}
        cols = {c["name"] for c in insp.get_columns("fall_alert_notifications")}
        for idx_name, idx_cols in FALL_ALERT_NOTIFICATION_INDEXES:
            if idx_name not in existing and all(c in cols for c in idx_cols):
                op.create_index(idx_name, "fall_alert_notifications", idx_cols, unique=False)


def downgrade() -> None:
    """Drop composite indexes on studio_sessions, fall_alerts, and fall_alert_notifications."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "fall_alert_notifications" in tables:
        existing = {idx["name"] for idx in insp.get_indexes("fall_alert_notifications")}
        for idx_name, _ in FALL_ALERT_NOTIFICATION_INDEXES:
            if idx_name in existing:
                op.drop_index(idx_name, table_name="fall_alert_notifications")

    if "fall_alerts" in tables:
        existing = {idx["name"] for idx in insp.get_indexes("fall_alerts")}
        for idx_name, _ in FALL_ALERT_INDEXES:
            if idx_name in existing:
                op.drop_index(idx_name, table_name="fall_alerts")

    if "studio_sessions" in tables:
        existing = {idx["name"] for idx in insp.get_indexes("studio_sessions")}
        for idx_name, _ in STUDIO_SESSION_INDEXES:
            if idx_name in existing:
                op.drop_index(idx_name, table_name="studio_sessions")
