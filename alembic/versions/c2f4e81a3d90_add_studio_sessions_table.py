"""add studio_sessions table and backfill historical data from DynamoDB

Revision ID: c2f4e81a3d90
Revises: 15c0a08421f5
Create Date: 2026-09-08 23:05:00.000000

"""
from datetime import datetime, timezone
import logging
from typing import Any, Sequence, Union
from uuid import UUID

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2f4e81a3d90'
down_revision: Union[str, Sequence[str], None] = '15c0a08421f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.migration")


def _backfill_sessions_from_dynamodb(conn: sa.Connection) -> None:
    """Scan DynamoDB telemetry items with session_id and provision studio_sessions."""
    try:
        import boto3
        from boto3.dynamodb.conditions import Attr
        from botocore.exceptions import BotoCoreError, ClientError
        from app.core.config import settings
    except Exception as exc:
        logger.warning(f"[backfill] boto3 or app settings not available ({exc}), skipping backfill.")
        return

    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())
    if "studio_sessions" not in tables or "users" not in tables:
        return

    # 1. Check if sessions already exist in PostgreSQL
    try:
        existing_count = conn.execute(sa.text("SELECT COUNT(*) FROM studio_sessions")).scalar() or 0
        if existing_count > 0:
            logger.info(f"[backfill] studio_sessions already contains {existing_count} records. Skipping.")
            return
    except Exception as exc:
        logger.warning(f"[backfill] Could not check existing count: {exc}")
        return

    # 2. Map device_id -> user_id from device_ownership
    device_to_user: dict[str, int] = {}
    if "device_ownership" in tables:
        try:
            ownership_rows = conn.execute(
                sa.text("SELECT device_id, user_id FROM device_ownership")
            ).fetchall()
            device_to_user = {row[0]: row[1] for row in ownership_rows}
        except Exception as exc:
            logger.warning(f"[backfill] Could not query device_ownership: {exc}")

    # Fallback if device is unassigned: first admin or first user
    fallback_user_id = None
    try:
        fallback_user_id = conn.execute(
            sa.text("SELECT id FROM users ORDER BY (role = 'admin') DESC, id ASC LIMIT 1")
        ).scalar()
    except Exception:
        pass

    if fallback_user_id is None:
        logger.info("[backfill] No users found in PostgreSQL. Skipping backfill.")
        return

    # 3. Scan DynamoDB telemetry table for items with session_id
    table_name = getattr(settings, "dynamodb_telemetry_table", "healthkicks_telemetry")
    region = getattr(settings, "aws_region", "eu-north-1")

    logger.info(f"[backfill] Fetching historical sessions from DynamoDB ({table_name})...")
    sessions: dict[str, dict[str, Any]] = {}
    try:
        dynamodb = boto3.resource("dynamodb", region_name=region)
        table = dynamodb.Table(table_name)

        scan_kwargs: dict[str, Any] = {
            "FilterExpression": Attr("session_id").exists(),
            "ProjectionExpression": "session_id, device_id, #lbl, #ts",
            "ExpressionAttributeNames": {"#lbl": "label", "#ts": "timestamp"},
        }

        while True:
            resp = table.scan(**scan_kwargs)
            for item in resp.get("Items", []):
                s_id = item.get("session_id")
                if not s_id:
                    continue
                s_id_str = str(s_id)
                ts = int(item["timestamp"])

                if s_id_str not in sessions:
                    sessions[s_id_str] = {
                        "id": s_id_str,
                        "device_id": item.get("device_id", "unknown"),
                        "label": item.get("label", "unknown"),
                        "min_ts": ts,
                        "max_ts": ts,
                        "count": 0,
                    }
                entry = sessions[s_id_str]
                entry["count"] += 1
                if ts < entry["min_ts"]:
                    entry["min_ts"] = ts
                if ts > entry["max_ts"]:
                    entry["max_ts"] = ts
                if entry["label"] == "unknown" and item.get("label"):
                    entry["label"] = item["label"]
                if entry["device_id"] == "unknown" and item.get("device_id"):
                    entry["device_id"] = item["device_id"]

            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key

    except (BotoCoreError, ClientError, Exception) as exc:
        logger.warning(f"[backfill] Could not scan DynamoDB ({exc}). Schema migration succeeded, backfill skipped.")
        return

    if not sessions:
        logger.info("[backfill] No studio sessions found in DynamoDB.")
        return

    # 4. Insert aggregated sessions into PostgreSQL
    insert_stmt = sa.text("""
        INSERT INTO studio_sessions (id, user_id, device_id, label, sample_count, duration_sec, created_at)
        VALUES (:id, :user_id, :device_id, :label, :sample_count, :duration_sec, :created_at)
        ON CONFLICT (id) DO NOTHING
    """)

    inserted_count = 0
    for sess in sessions.values():
        try:
            sess_uuid = UUID(sess["id"])
        except (ValueError, TypeError):
            continue

        dev_id = sess["device_id"]
        user_id = device_to_user.get(dev_id, fallback_user_id)
        min_ts = sess["min_ts"]
        max_ts = sess["max_ts"]
        duration_sec = round(max((max_ts - min_ts) / 1_000_000.0, 5.0), 2)
        created_at = datetime.fromtimestamp(min_ts / 1_000_000, tz=timezone.utc)

        try:
            conn.execute(
                insert_stmt,
                {
                    "id": sess_uuid,
                    "user_id": user_id,
                    "device_id": dev_id,
                    "label": sess["label"],
                    "sample_count": sess["count"],
                    "duration_sec": duration_sec,
                    "created_at": created_at,
                },
            )
            inserted_count += 1
        except Exception as exc:
            logger.warning(f"[backfill] Failed inserting session {sess_uuid}: {exc}")

    logger.info(f"[backfill] Successfully provisioned {inserted_count} studio sessions into PostgreSQL.")


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "studio_sessions" not in tables:
        op.create_table(
            'studio_sessions',
            sa.Column('id', sa.Uuid(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('device_id', sa.String(length=128), nullable=False),
            sa.Column('label', sa.String(length=64), nullable=False),
            sa.Column('sample_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('duration_sec', sa.Float(), nullable=False, server_default='5.0'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(op.f('ix_studio_sessions_device_id'), 'studio_sessions', ['device_id'], unique=False)
        op.create_index(op.f('ix_studio_sessions_label'), 'studio_sessions', ['label'], unique=False)
        op.create_index(op.f('ix_studio_sessions_user_id'), 'studio_sessions', ['user_id'], unique=False)

    # Backfill historical data from DynamoDB if present
    _backfill_sessions_from_dynamodb(conn)


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "studio_sessions" in tables:
        op.drop_index(op.f('ix_studio_sessions_user_id'), table_name='studio_sessions')
        op.drop_index(op.f('ix_studio_sessions_label'), table_name='studio_sessions')
        op.drop_index(op.f('ix_studio_sessions_device_id'), table_name='studio_sessions')
        op.drop_table('studio_sessions')


