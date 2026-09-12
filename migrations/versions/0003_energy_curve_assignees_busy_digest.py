"""energy curve, assignees, calendar busy cache, morning digest

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-12 21:40:00.000000

SQLite note: plain ``ALTER TABLE ... ADD COLUMN`` only.  Never use
``batch_alter_table`` here - it rebuilds the table and the production database
has rows with dangling foreign keys (see 0002).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def _cols(table: str) -> set:
    bind = op.get_bind()
    return {r[1] for r in bind.exec_driver_sql(f"PRAGMA table_info({table})")}


def _add(table: str, col: sa.Column) -> None:
    if col.name not in _cols(table):
        op.add_column(table, col)


def upgrade() -> None:
    bind = op.get_bind()
    for t in ("tasks", "task_reflections", "user_profiles", "calendar_connections"):
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS _alembic_tmp_{t}")

    _add('tasks', sa.Column('scheduled_start', sa.String(length=5), nullable=True))
    _add('tasks', sa.Column('assigned_to_id', sa.String(length=36), nullable=True))
    bind.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_tasks_assigned_to_id ON tasks (assigned_to_id)")

    _add('task_reflections', sa.Column('hour_of_day', sa.Integer(), nullable=True))

    _add('user_profiles', sa.Column('digest_json', sa.Text(), nullable=False, server_default='{}'))

    _add('calendar_connections', sa.Column('busy_enabled', sa.Boolean(), nullable=False, server_default=sa.true()))
    _add('calendar_connections', sa.Column('busy_cache_json', sa.Text(), nullable=False, server_default='{}'))
    _add('calendar_connections', sa.Column('busy_fetched_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    # Columns are additive and harmless; dropping them on SQLite would rebuild
    # the tables (the exact operation that failed in production for 0002), so
    # the downgrade intentionally leaves them in place.
    pass
