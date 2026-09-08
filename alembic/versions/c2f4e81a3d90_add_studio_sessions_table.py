"""add studio_sessions table

Revision ID: c2f4e81a3d90
Revises: 15c0a08421f5
Create Date: 2026-09-08 23:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c2f4e81a3d90'
down_revision: Union[str, Sequence[str], None] = '15c0a08421f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "studio_sessions" not in tables:
        op.create_table(
            'studio_sessions',
            sa.Column('id', sa.Uuid(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('device_id', sa.String(length=128), nullable=False),
            sa.Column('label', sa.String(length=64), nullable=False),
            sa.Column('sample_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('duration_sec', sa.Float(), nullable=False, server_default='5.0'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(op.f('ix_studio_sessions_device_id'), 'studio_sessions', ['device_id'], unique=False)
        op.create_index(op.f('ix_studio_sessions_label'), 'studio_sessions', ['label'], unique=False)
        op.create_index(op.f('ix_studio_sessions_user_id'), 'studio_sessions', ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    insp = sa.inspect(conn)
    tables = set(insp.get_table_names())

    if "studio_sessions" in tables:
        op.drop_index(op.f('ix_studio_sessions_user_id'), table_name='studio_sessions')
        op.drop_index(op.f('ix_studio_sessions_label'), table_name='studio_sessions')
        op.drop_index(op.f('ix_studio_sessions_device_id'), table_name='studio_sessions')
        op.drop_table('studio_sessions')
