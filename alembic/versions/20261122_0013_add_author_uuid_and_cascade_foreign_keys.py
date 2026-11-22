"""Add author_uuid to studio_sessions and configure cascade foreign keys for GDPR account deletion.

Revision ID: 20261122_0013
Revises: 20261120_0012
Create Date: 2026-11-22 15:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "20261122_0013"
down_revision: str | Sequence[str] | None = "20261120_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add author_uuid column, backfill from users.uuid, and configure cascading foreign keys."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    # 1. Add author_uuid column to studio_sessions with index if not present
    if "studio_sessions" in tables:
        cols = {c["name"] for c in insp.get_columns("studio_sessions")}
        if "author_uuid" not in cols:
            op.add_column("studio_sessions", sa.Column("author_uuid", sa.Uuid(), nullable=True))
            indexes = {ix["name"] for ix in insp.get_indexes("studio_sessions")}
            if "ix_studio_sessions_author_uuid" not in indexes:
                op.create_index(
                    "ix_studio_sessions_author_uuid",
                    "studio_sessions",
                    ["author_uuid"],
                    unique=False,
                )

    # 2. Backfill existing studio_sessions.author_uuid from users.uuid
    if "studio_sessions" in tables and "users" in tables:
        if bind.dialect.name == "postgresql":
            op.execute(
                "UPDATE studio_sessions SET author_uuid = users.uuid FROM users WHERE studio_sessions.user_id = users.id AND studio_sessions.author_uuid IS NULL;"
            )
        else:
            # SQLite compatible backfill
            op.execute(
                "UPDATE studio_sessions SET author_uuid = (SELECT uuid FROM users WHERE users.id = studio_sessions.user_id) WHERE user_id IS NOT NULL AND author_uuid IS NULL;"
            )

    # 3. Configure Foreign Keys for PostgreSQL
    if bind.dialect.name == "postgresql":
        # Drop existing FK constraint on studio_sessions.user_id if present
        op.execute("""
            DO $$
            DECLARE
                r RECORD;
            BEGIN
                FOR r IN (
                    SELECT tc.constraint_name
                    FROM information_schema.table_constraints AS tc
                    JOIN information_schema.key_column_usage AS kcu
                      ON tc.constraint_name = kcu.constraint_name
                    WHERE tc.table_name = 'studio_sessions'
                      AND tc.constraint_type = 'FOREIGN KEY'
                      AND kcu.column_name = 'user_id'
                ) LOOP
                    EXECUTE 'ALTER TABLE studio_sessions DROP CONSTRAINT ' || quote_ident(r.constraint_name);
                END LOOP;
            END $$;
        """)
        op.alter_column("studio_sessions", "user_id", existing_type=sa.Integer(), nullable=True)
        op.create_foreign_key(
            "fk_studio_sessions_user_id_users",
            "studio_sessions",
            "users",
            ["user_id"],
            ["id"],
            ondelete="SET NULL",
        )

        # Drop existing FK constraint on device_ownership.user_id if present
        op.execute("""
            DO $$
            DECLARE
                r RECORD;
            BEGIN
                FOR r IN (
                    SELECT tc.constraint_name
                    FROM information_schema.table_constraints AS tc
                    JOIN information_schema.key_column_usage AS kcu
                      ON tc.constraint_name = kcu.constraint_name
                    WHERE tc.table_name = 'device_ownership'
                      AND tc.constraint_type = 'FOREIGN KEY'
                      AND kcu.column_name = 'user_id'
                ) LOOP
                    EXECUTE 'ALTER TABLE device_ownership DROP CONSTRAINT ' || quote_ident(r.constraint_name);
                END LOOP;
            END $$;
        """)
        op.create_foreign_key(
            "fk_device_ownership_user_id_users",
            "device_ownership",
            "users",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )
    else:
        # SQLite batch mode
        with op.batch_alter_table("studio_sessions") as batch_op:
            batch_op.alter_column("user_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    """Revert foreign key constraints and drop author_uuid."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint("fk_device_ownership_user_id_users", "device_ownership", type_="foreignkey")
        op.drop_constraint("fk_studio_sessions_user_id_users", "studio_sessions", type_="foreignkey")
        op.create_foreign_key(
            "studio_sessions_user_id_fkey",
            "studio_sessions",
            "users",
            ["user_id"],
            ["id"],
        )

    op.drop_index("ix_studio_sessions_author_uuid", table_name="studio_sessions")
    op.drop_column("studio_sessions", "author_uuid")
