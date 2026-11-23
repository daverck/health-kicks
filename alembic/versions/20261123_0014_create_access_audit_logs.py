"""Create access_audit_logs table with database-level immutability triggers.

Revision ID: 20261123_0014
Revises: 20261122_0013
Create Date: 2026-11-23 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "20261123_0014"
down_revision: str | Sequence[str] | None = "20261122_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create access_audit_logs table, indexes, and immutability triggers."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "access_audit_logs" not in tables:
        op.create_table(
            "access_audit_logs",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column(
                "uuid",
                sa.Uuid().with_variant(postgresql.UUID(as_uuid=True), "postgresql"),
                nullable=False,
            ),
            sa.Column(
                "actor_uuid",
                sa.Uuid().with_variant(postgresql.UUID(as_uuid=True), "postgresql"),
                nullable=False,
            ),
            sa.Column("actor_role", sa.String(length=32), nullable=False),
            sa.Column("actor_email", sa.String(length=320), nullable=True),
            sa.Column(
                "patient_uuid",
                sa.Uuid().with_variant(postgresql.UUID(as_uuid=True), "postgresql"),
                nullable=False,
            ),
            sa.Column("patient_pseudonym", sa.String(length=32), nullable=True),
            sa.Column("action", sa.String(length=16), nullable=False),
            sa.Column("resource", sa.String(length=64), nullable=False),
            sa.Column("resource_id", sa.String(length=128), nullable=True),
            sa.Column("ip_address", sa.String(length=45), nullable=True),
            sa.Column("user_agent", sa.String(length=255), nullable=True),
            sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_access_audit_logs_uuid", "access_audit_logs", ["uuid"], unique=True)
        op.create_index(
            "ix_access_audit_logs_actor_uuid", "access_audit_logs", ["actor_uuid"], unique=False
        )
        op.create_index(
            "ix_access_audit_logs_patient_uuid", "access_audit_logs", ["patient_uuid"], unique=False
        )
        op.create_index(
            "ix_access_audit_logs_patient_pseudonym",
            "access_audit_logs",
            ["patient_pseudonym"],
            unique=False,
        )
        op.create_index(
            "ix_access_audit_logs_created_at_utc",
            "access_audit_logs",
            ["created_at_utc"],
            unique=False,
        )
        op.create_index(
            "ix_audit_patient_created",
            "access_audit_logs",
            ["patient_uuid", "created_at_utc"],
            unique=False,
        )
        op.create_index(
            "ix_audit_actor_created",
            "access_audit_logs",
            ["actor_uuid", "created_at_utc"],
            unique=False,
        )
        op.create_index(
            "ix_audit_resource_action",
            "access_audit_logs",
            ["resource", "action", "created_at_utc"],
            unique=False,
        )

    # Database-level immutability trigger / rule
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION prevent_access_audit_log_modification()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'access_audit_logs table is immutable: updates and deletions are strictly prohibited by HIPAA and GDPR audit policy.';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            """
            DROP TRIGGER IF EXISTS trg_immutable_access_audit_logs ON access_audit_logs;
            CREATE TRIGGER trg_immutable_access_audit_logs
            BEFORE UPDATE OR DELETE ON access_audit_logs
            FOR EACH ROW EXECUTE FUNCTION prevent_access_audit_log_modification();
            """
        )
    elif bind.dialect.name == "sqlite":
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_no_update_access_audit_logs
            BEFORE UPDATE ON access_audit_logs
            BEGIN
                SELECT RAISE(ABORT, 'access_audit_logs table is immutable: updates are strictly prohibited.');
            END;
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_no_delete_access_audit_logs
            BEFORE DELETE ON access_audit_logs
            BEGIN
                SELECT RAISE(ABORT, 'access_audit_logs table is immutable: deletions are strictly prohibited.');
            END;
            """
        )


def downgrade() -> None:
    """Drop immutability triggers and access_audit_logs table."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_immutable_access_audit_logs ON access_audit_logs;")
        op.execute("DROP FUNCTION IF EXISTS prevent_access_audit_log_modification();")
    elif bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_no_update_access_audit_logs;")
        op.execute("DROP TRIGGER IF EXISTS trg_no_delete_access_audit_logs;")

    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "access_audit_logs" in tables:
        op.drop_table("access_audit_logs")
