"""board invitations

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-12 22:10:00.000000

SQLite note: create-if-missing only, never ``batch_alter_table`` (see 0002/0003).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TABLE IF EXISTS _alembic_tmp_board_invites")
    if not bind.dialect.has_table(bind, "board_invites"):
        op.create_table(
            'board_invites',
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('board_id', sa.String(length=36), nullable=False),
            sa.Column('email', sa.String(length=254), nullable=False),
            sa.Column('role', sa.String(length=16), nullable=False),
            sa.Column('token_hash', sa.String(length=128), nullable=False),
            sa.Column('token_prefix', sa.String(length=12), nullable=False),
            sa.Column('invited_by_id', sa.String(length=36), nullable=True),
            sa.Column('message', sa.String(length=500), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('accepted_at', sa.DateTime(), nullable=True),
            sa.Column('accepted_by_id', sa.String(length=36), nullable=True),
            sa.Column('declined_at', sa.DateTime(), nullable=True),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['board_id'], ['boards.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
    bind.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ix_board_invites_token ON board_invites (token_hash)")
    bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_board_invites_email ON board_invites (email)")
    bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_board_invites_board ON board_invites (board_id)")


def downgrade() -> None:
    op.drop_table('board_invites')
