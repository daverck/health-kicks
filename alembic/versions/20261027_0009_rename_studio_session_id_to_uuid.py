"""rename studio_session id to uuid

Revision ID: 20261027_0009
Revises: 20261027_0008
Create Date: 2026-10-27 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261027_0009"
down_revision: str | Sequence[str] | None = "20261027_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename primary key id to uuid in studio_sessions table."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "studio_sessions" in tables:
        cols = {c["name"] for c in insp.get_columns("studio_sessions")}
        if "id" in cols and "uuid" not in cols:
            with op.batch_alter_table("studio_sessions") as batch_op:
                batch_op.alter_column("id", new_column_name="uuid")


def downgrade() -> None:
    """Revert primary key uuid to id in studio_sessions table."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "studio_sessions" in tables:
        cols = {c["name"] for c in insp.get_columns("studio_sessions")}
        if "uuid" in cols and "id" not in cols:
            with op.batch_alter_table("studio_sessions") as batch_op:
                batch_op.alter_column("uuid", new_column_name="id")

