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
    pass
    conn = op.get_bind()
    insp = sa.inspect(conn)
    columns = {col["name"] for col in insp.get_columns("users")}

    has_azure_sub = "azure_sub" in columns
    has_auth_provider = "auth_provider" in columns

    if not has_azure_sub or not has_auth_provider:
        with op.batch_alter_table("users", schema=None) as batch_op:
            batch_op.alter_column(
                "google_sub",
                existing_type=sa.String(length=255),
                nullable=True,
            )
            if not has_azure_sub:
                batch_op.add_column(sa.Column("azure_sub", sa.String(length=255), nullable=True))
                batch_op.create_index(batch_op.f("ix_users_azure_sub"), ["azure_sub"], unique=True)
            if not has_auth_provider:
                batch_op.add_column(sa.Column("auth_provider", sa.String(length=32), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    pass
    conn = op.get_bind()
    insp = sa.inspect(conn)
    columns = {col["name"] for col in insp.get_columns("users")}

    with op.batch_alter_table("users", schema=None) as batch_op:
        if "azure_sub" in columns:
            batch_op.drop_index(batch_op.f("ix_users_azure_sub"))
            batch_op.drop_column("azure_sub")
        if "auth_provider" in columns:
            batch_op.drop_column("auth_provider")
        batch_op.alter_column(
            "google_sub",
            existing_type=sa.String(length=255),
            nullable=False,
        )
