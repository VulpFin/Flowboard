"""when and by whom a task was assigned

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-12 22:40:00.000000

SQLite note: plain ``ALTER TABLE ... ADD COLUMN`` only (see 0002/0003).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def _cols(table: str) -> set:
    bind = op.get_bind()
    return {r[1] for r in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def _add(table: str, col: sa.Column) -> None:
    if col.name not in _cols(table):
        op.add_column(table, col)


def upgrade() -> None:
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS _alembic_tmp_tasks")
    _add('tasks', sa.Column('assigned_at', sa.DateTime(), nullable=True))
    _add('tasks', sa.Column('assigned_by_id', sa.String(length=36), nullable=True))


def downgrade() -> None:
    # additive and harmless; dropping columns on SQLite rebuilds the table
    pass
