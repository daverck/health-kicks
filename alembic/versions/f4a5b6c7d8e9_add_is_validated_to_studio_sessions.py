"""add is_validated column to studio_sessions

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-09-13 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a5b6c7d8e9'
down_revision: Union[str, Sequence[str], None] = 'e3f4a5b6c7d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add is_validated column to studio_sessions initialized to True for legacy sessions."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "studio_sessions" in tables:
        cols = {c["name"] for c in insp.get_columns("studio_sessions")}
        if "is_validated" not in cols:
            op.add_column(
                "studio_sessions",
                sa.Column(
                    "is_validated",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.true(),
                ),
            )
        existing_indices = {idx["name"] for idx in insp.get_indexes("studio_sessions")}
        if "ix_studio_sessions_is_validated" not in existing_indices:
            op.create_index(
                "ix_studio_sessions_is_validated",
                "studio_sessions",
                ["is_validated"],
                unique=False,
            )


def downgrade() -> None:
    """Drop is_validated column and index from studio_sessions."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "studio_sessions" in tables:
        existing_indices = {idx["name"] for idx in insp.get_indexes("studio_sessions")}
        if "ix_studio_sessions_is_validated" in existing_indices:
            op.drop_index("ix_studio_sessions_is_validated", table_name="studio_sessions")

        cols = {c["name"] for c in insp.get_columns("studio_sessions")}
        if "is_validated" in cols:
            if conn.dialect.name == "sqlite":
                with op.batch_alter_table("studio_sessions") as batch_op:
                    batch_op.drop_column("is_validated")
            else:
                op.drop_column("studio_sessions", "is_validated")
