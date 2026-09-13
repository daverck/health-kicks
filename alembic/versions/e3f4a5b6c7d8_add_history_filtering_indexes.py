"""add history filtering indexes for activity events, haptic log, and studio sessions

Revision ID: e3f4a5b6c7d8
Revises: d1e2f3a4b5c6
Create Date: 2026-09-13 01:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3f4a5b6c7d8'
down_revision: Union[str, Sequence[str], None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add composite and date indexes to optimize history queries."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    # 1. ActivityEvent composite indexes
    if "activity_events" in tables:
        existing_indices = {idx["name"] for idx in insp.get_indexes("activity_events")}
        if "ix_activity_events_device_id_timestamp_utc" not in existing_indices:
            op.create_index(
                "ix_activity_events_device_id_timestamp_utc",
                "activity_events",
                ["device_id", "timestamp_utc"],
                unique=False,
            )
        if "ix_activity_events_device_id_event_type" not in existing_indices:
            op.create_index(
                "ix_activity_events_device_id_event_type",
                "activity_events",
                ["device_id", "event_type"],
                unique=False,
            )

    # 2. HapticLog composite index
    if "haptic_commands_log" in tables:
        existing_indices = {idx["name"] for idx in insp.get_indexes("haptic_commands_log")}
        if "ix_haptic_log_device_id_triggered_at" not in existing_indices:
            op.create_index(
                "ix_haptic_log_device_id_triggered_at",
                "haptic_commands_log",
                ["device_id", "triggered_at_utc"],
                unique=False,
            )

    # 3. StudioSession created_at index
    if "studio_sessions" in tables:
        existing_indices = {idx["name"] for idx in insp.get_indexes("studio_sessions")}
        if "ix_studio_sessions_created_at" not in existing_indices:
            op.create_index(
                "ix_studio_sessions_created_at",
                "studio_sessions",
                ["created_at"],
                unique=False,
            )


def downgrade() -> None:
    """Drop history filtering indexes."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "activity_events" in tables:
        existing_indices = {idx["name"] for idx in insp.get_indexes("activity_events")}
        if "ix_activity_events_device_id_timestamp_utc" in existing_indices:
            op.drop_index("ix_activity_events_device_id_timestamp_utc", table_name="activity_events")
        if "ix_activity_events_device_id_event_type" in existing_indices:
            op.drop_index("ix_activity_events_device_id_event_type", table_name="activity_events")

    if "haptic_commands_log" in tables:
        existing_indices = {idx["name"] for idx in insp.get_indexes("haptic_commands_log")}
        if "ix_haptic_log_device_id_triggered_at" in existing_indices:
            op.drop_index("ix_haptic_log_device_id_triggered_at", table_name="haptic_commands_log")

    if "studio_sessions" in tables:
        existing_indices = {idx["name"] for idx in insp.get_indexes("studio_sessions")}
        if "ix_studio_sessions_created_at" in existing_indices:
            op.drop_index("ix_studio_sessions_created_at", table_name="studio_sessions")

