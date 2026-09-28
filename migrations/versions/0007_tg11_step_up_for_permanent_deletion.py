"""bind a fresh TG11 reauthentication to a destructive account purge

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-17 01:25:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {row[1] for row in op.get_bind().exec_driver_sql("PRAGMA table_info(user_sessions)")}
    if "reauthenticated_at" not in cols:
        op.add_column("user_sessions", sa.Column("reauthenticated_at", sa.DateTime(), nullable=True))
    if "reauth_target_id" not in cols:
        op.add_column("user_sessions", sa.Column("reauth_target_id", sa.String(length=36), nullable=True))


def downgrade() -> None:
    pass
