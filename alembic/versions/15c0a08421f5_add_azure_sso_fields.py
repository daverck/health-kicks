"""add_azure_sso_fields

Revision ID: 15c0a08421f5
Revises: ae0c7296d51e
Create Date: 2026-09-04 13:03:33.967013

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '15c0a08421f5'
down_revision: Union[str, Sequence[str], None] = 'ae0c7296d51e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    columns = {col["name"] for col in insp.get_columns("users")}

    has_azure_sub = "azure_sub" in columns

    if not has_azure_sub:
        with op.batch_alter_table("users", schema=None) as batch_op:
            batch_op.alter_column(
                "google_sub",
                existing_type=sa.String(length=255),
                nullable=True,
            )
            batch_op.add_column(sa.Column("azure_sub", sa.String(length=255), nullable=True))
            batch_op.create_index(batch_op.f("ix_users_azure_sub"), ["azure_sub"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    columns = {col["name"] for col in insp.get_columns("users")}

    with op.batch_alter_table("users", schema=None) as batch_op:
        if "azure_sub" in columns:
            batch_op.drop_index(batch_op.f("ix_users_azure_sub"))
            batch_op.drop_column("azure_sub")
        batch_op.alter_column(
            "google_sub",
            existing_type=sa.String(length=255),
            nullable=False,
        )

