# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Calendar integration models (provider-agnostic)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .common import TimestampMixin, UUIDPrimaryKeyMixin, utcnow


class CalendarConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An OAuth connection to an external calendar account (google / microsoft).
    Tokens are stored encrypted (same envelope as AI credentials)."""

    __tablename__ = "calendar_connections"
    __table_args__ = (Index("ix_calconn_user", "user_id"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)  # google|microsoft
    account_email: Mapped[str] = mapped_column(String(254), default="", nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    token_blob: Mapped[bytes] = mapped_column(nullable=False)  # encrypted JSON {access_token, refresh_token, expires_at, scope}
    key_version: Mapped[int] = mapped_column(default=1, nullable=False)
    scopes: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    target_calendar_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    target_calendar_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    calendars_cache_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="connected", nullable=False)  # connected|error|revoked
    status_message: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Busy-time (free/busy) capacity subtraction
    busy_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    busy_cache_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)  # {"from":..,"to":..,"intervals":[[iso,iso]..]}
    busy_fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class CalendarEventLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Persistent link between a Flowboard task and an external calendar event."""

    __tablename__ = "calendar_event_links"
    __table_args__ = (
        UniqueConstraint("task_id", "connection_id", name="uq_eventlink_task_connection"),
        Index("ix_eventlink_board", "board_id"),
    )

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    connection_id: Mapped[str] = mapped_column(ForeignKey("calendar_connections.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    external_calendar_id: Mapped[str] = mapped_column(String(255), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    external_url: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    sync_status: Mapped[str] = mapped_column(String(16), default="synced", nullable=False)  # synced|pending|error|orphaned
    sync_direction: Mapped[str] = mapped_column(String(16), default="push", nullable=False)  # push (flowboard -> external)
    auto_update: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)  # hash of last pushed content


class CalendarFeedToken(UUIDPrimaryKeyMixin, Base):
    """Secret token for an iCalendar subscription feed of one board."""

    __tablename__ = "calendar_feed_tokens"
    __table_args__ = (UniqueConstraint("board_id", "user_id", name="uq_feed_board_user"),)

    board_id: Mapped[str] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(12), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    include_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
