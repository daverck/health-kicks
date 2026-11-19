"""create ml_models table for MLOps model registry

Revision ID: 20261119_0011
Revises: 20261113_0010
Create Date: 2026-11-19 18:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261119_0011"
down_revision: str | Sequence[str] | None = "20261113_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ml_models table and indexes if not already present."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "ml_models" not in tables:
        op.create_table(
            "ml_models",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("uuid", sa.UUID(as_uuid=True), nullable=False),
            sa.Column("version", sa.String(length=32), nullable=False),
            sa.Column("model_name", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("git_commit", sa.String(length=64), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("window_size_sec", sa.Float(), nullable=False, server_default="3.0"),
            sa.Column("window_step_sec", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column(
                "selection_criteria",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
            ),
            sa.Column(
                "dataset_summary",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
            ),
            sa.Column(
                "metrics",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
            ),
            sa.Column("c_header_size_bytes", sa.Integer(), nullable=True),
            sa.Column("flash_usage_pct", sa.Float(), nullable=True),
            sa.Column("ram_usage_pct", sa.Float(), nullable=True),
        )
        op.create_index("ix_ml_models_uuid", "ml_models", ["uuid"], unique=True)
        op.create_index("ix_ml_models_version", "ml_models", ["version"], unique=True)
        op.create_index("ix_ml_models_is_active", "ml_models", ["is_active"], unique=False)
        op.create_index("ix_ml_models_created_at", "ml_models", ["created_at"], unique=False)


def downgrade() -> None:
    """Drop ml_models table if present."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "ml_models" in tables:
        op.drop_table("ml_models")
