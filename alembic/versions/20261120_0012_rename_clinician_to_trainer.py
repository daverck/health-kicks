"""Rename clinician role to trainer in userrole enum and users table.

Revision ID: 20261120_0012
Revises: 20261119_0011
Create Date: 2026-11-20 20:00:00.000000
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20261120_0012"
down_revision = "20261119_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'trainer'"))
    op.execute(sa.text("UPDATE users SET role = 'trainer' WHERE role = 'clinician'"))


def downgrade() -> None:
    op.execute(sa.text("UPDATE users SET role = 'clinician' WHERE role = 'trainer'"))
