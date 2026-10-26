"""add uuid and pseudonym columns to users, fall_alerts, and alert_contacts

Revision ID: 20261026_0007
Revises: 20261024_0006
Create Date: 2026-10-26 21:00:00.000000

"""
from collections.abc import Sequence
import uuid

import sqlalchemy as sa
from alembic import op

from app.core.pseudonymization import get_subject_pseudonym

# revision identifiers, used by Alembic.
revision: str = "20261026_0007"
down_revision: str | Sequence[str] | None = "20261024_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add uuid & pseudonym to users, and uuid to fall_alerts & alert_contacts with backfill."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    # 1. users: add uuid and pseudonym
    if "users" in tables:
        user_cols = {c["name"] for c in insp.get_columns("users")}
        user_indices = {i["name"] for i in insp.get_indexes("users")}

        if "uuid" not in user_cols:
            op.add_column("users", sa.Column("uuid", sa.UUID(as_uuid=True), nullable=True))

        if "pseudonym" not in user_cols:
            op.add_column("users", sa.Column("pseudonym", sa.String(length=32), nullable=True))

        # Backfill existing users
        users = conn.execute(sa.text("SELECT id, uuid, pseudonym FROM users")).fetchall()
        for u in users:
            uid = u[0]
            existing_uuid = u[1]
            existing_pseudo = u[2]

            row_uuid = existing_uuid if existing_uuid else uuid.uuid4()
            row_pseudo = (
                existing_pseudo
                if existing_pseudo
                else get_subject_pseudonym(str(row_uuid))
            )

            conn.execute(
                sa.text("UPDATE users SET uuid = :u, pseudonym = :p WHERE id = :id"),
                {"u": str(row_uuid), "p": row_pseudo, "id": uid},
            )

        # Enforce batch alter constraints / indexes
        with op.batch_alter_table("users") as batch_op:
            batch_op.alter_column("uuid", nullable=False)
            batch_op.alter_column("pseudonym", nullable=False)
            if "ix_users_uuid" not in user_indices:
                batch_op.create_index("ix_users_uuid", ["uuid"], unique=True)
            if "ix_users_pseudonym" not in user_indices:
                batch_op.create_index("ix_users_pseudonym", ["pseudonym"], unique=True)

    # 2. alert_contacts: add uuid
    if "alert_contacts" in tables:
        ac_cols = {c["name"] for c in insp.get_columns("alert_contacts")}
        ac_indices = {i["name"] for i in insp.get_indexes("alert_contacts")}
        if "uuid" not in ac_cols:
            op.add_column("alert_contacts", sa.Column("uuid", sa.UUID(as_uuid=True), nullable=True))

            contacts = conn.execute(sa.text("SELECT id FROM alert_contacts")).fetchall()
            for c in contacts:
                cid = c[0]
                conn.execute(
                    sa.text("UPDATE alert_contacts SET uuid = :u WHERE id = :id"),
                    {"u": str(uuid.uuid4()), "id": cid},
                )

            with op.batch_alter_table("alert_contacts") as batch_op:
                batch_op.alter_column("uuid", nullable=False)
                if "ix_alert_contacts_uuid" not in ac_indices:
                    batch_op.create_index("ix_alert_contacts_uuid", ["uuid"], unique=True)

    # 3. fall_alerts: add uuid
    if "fall_alerts" in tables:
        fa_cols = {c["name"] for c in insp.get_columns("fall_alerts")}
        fa_indices = {i["name"] for i in insp.get_indexes("fall_alerts")}
        if "uuid" not in fa_cols:
            op.add_column("fall_alerts", sa.Column("uuid", sa.UUID(as_uuid=True), nullable=True))

            alerts = conn.execute(sa.text("SELECT id FROM fall_alerts")).fetchall()
            for a in alerts:
                aid = a[0]
                conn.execute(
                    sa.text("UPDATE fall_alerts SET uuid = :u WHERE id = :id"),
                    {"u": str(uuid.uuid4()), "id": aid},
                )

            with op.batch_alter_table("fall_alerts") as batch_op:
                batch_op.alter_column("uuid", nullable=False)
                if "ix_fall_alerts_uuid" not in fa_indices:
                    batch_op.create_index("ix_fall_alerts_uuid", ["uuid"], unique=True)


def downgrade() -> None:
    """Drop uuid and pseudonym columns."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "fall_alerts" in tables:
        fa_cols = {c["name"] for c in insp.get_columns("fall_alerts")}
        fa_indices = {i["name"] for i in insp.get_indexes("fall_alerts")}
        if "uuid" in fa_cols:
            with op.batch_alter_table("fall_alerts") as batch_op:
                if "ix_fall_alerts_uuid" in fa_indices:
                    batch_op.drop_index("ix_fall_alerts_uuid")
                batch_op.drop_column("uuid")

    if "alert_contacts" in tables:
        ac_cols = {c["name"] for c in insp.get_columns("alert_contacts")}
        ac_indices = {i["name"] for i in insp.get_indexes("alert_contacts")}
        if "uuid" in ac_cols:
            with op.batch_alter_table("alert_contacts") as batch_op:
                if "ix_alert_contacts_uuid" in ac_indices:
                    batch_op.drop_index("ix_alert_contacts_uuid")
                batch_op.drop_column("uuid")

    if "users" in tables:
        user_cols = {c["name"] for c in insp.get_columns("users")}
        user_indices = {i["name"] for i in insp.get_indexes("users")}
        with op.batch_alter_table("users") as batch_op:
            if "pseudonym" in user_cols:
                if "ix_users_pseudonym" in user_indices:
                    batch_op.drop_index("ix_users_pseudonym")
                batch_op.drop_column("pseudonym")
            if "uuid" in user_cols:
                if "ix_users_uuid" in user_indices:
                    batch_op.drop_index("ix_users_uuid")
                batch_op.drop_column("uuid")
