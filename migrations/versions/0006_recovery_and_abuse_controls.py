"""recoverable deletes, operator audit log, and source-IP blocks

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-17 00:55:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def _cols(table: str) -> set:
    return {row[1] for row in op.get_bind().exec_driver_sql(f"PRAGMA table_info({table})")}


def _add(table: str, column: sa.Column) -> None:
    if column.name not in _cols(table):
        op.add_column(table, column)


def upgrade() -> None:
    for table in ("users", "boards", "ai_provider_credentials"):
        _add(table, sa.Column("deleted_at", sa.DateTime(), nullable=True))
        _add(table, sa.Column("deleted_by_id", sa.String(length=36), nullable=True))
    op.create_table(
        "ip_blocks",
        sa.Column("cidr", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by_id", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cidr", name="uq_ip_block_cidr"),
    )
    op.create_table(
        "admin_audit_events",
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_audit_subject_time", "admin_audit_events", ["subject_type", "subject_id", "created_at"], unique=False)


def downgrade() -> None:
    # Recovery metadata is intentionally retained; SQLite column drops rebuild tables.
    pass
