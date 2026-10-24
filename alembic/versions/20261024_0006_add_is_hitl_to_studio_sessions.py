"""add is_hitl to studio_sessions table

Revision ID: 20261024_0006
Revises: 20261023_0005
Create Date: 2026-10-24 23:35:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261024_0006"
down_revision: str | Sequence[str] | None = "20261023_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add is_hitl column and index to studio_sessions table."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "studio_sessions" in tables:
        cols = {c["name"] for c in insp.get_columns("studio_sessions")}
        if "is_hitl" not in cols:
            op.add_column(
                "studio_sessions",
                sa.Column(
                    "is_hitl",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("false"),
                ),
            )
            op.create_index(
                "ix_studio_sessions_is_hitl",
                "studio_sessions",
                ["is_hitl"],
            )


def downgrade() -> None:
    """Remove is_hitl column and index from studio_sessions table."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "studio_sessions" in tables:
        cols = {c["name"] for c in insp.get_columns("studio_sessions")}
        if "is_hitl" in cols:
            op.drop_index("ix_studio_sessions_is_hitl", table_name="studio_sessions")
            op.drop_column("studio_sessions", "is_hitl")

