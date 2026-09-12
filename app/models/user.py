# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Identity vs. profile.

`User`        - authentication identity (mirrors the FreeParty/Shop/TG11 model:
                UUID id, unique lower-cased email, username, Django-compatible
                password hash, account state).  When TG11 SSO is enabled the
                user row is linked to the global TG11 identity through
                `IdentityLink`; local passwords become optional.
`UserProfile` - Flowboard-specific settings (default board, AI defaults,
                calendar preferences).  One row per user.
`IdentityLink`- maps this local account to an external identity provider
                subject (TG11 OIDC now, others later).  The TG11 user UUID
                (`subject`) is the stable cross-service identifier.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from .common import TimestampMixin, UUIDPrimaryKeyMixin, utcnow


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # None => no local password (SSO only)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="active", nullable=False)  # active|pending_verification|limited|suspended
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Stable TG11 identity UUID once linked (denormalised from IdentityLink for fast lookup)
    tg11_user_id: Mapped[Optional[str]] = mapped_column(String(36), unique=True, nullable=True)

    profile: Mapped["UserProfile"] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")
    identity_links: Mapped[list["IdentityLink"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username} {self.id}>"

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)


class UserProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_profiles"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    default_board_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    week_start: Mapped[int] = mapped_column(default=1, nullable=False)  # 0=Sunday 1=Monday
    work_day_minutes: Mapped[int] = mapped_column(default=480, nullable=False)
    # AI defaults (account-wide).  Format "provider:model" or empty.
    default_ai_model: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    ai_fallback_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_fallback_model: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    ai_auto_tag_on_create: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_usage_tracking: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Calendar preferences
    calendar_default_connection_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    calendar_default_duration_min: Mapped[int] = mapped_column(default=60, nullable=False)
    # Board defaults for new boards (JSON text)
    board_defaults_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # Work schedule: {"days": {"0": {"enabled": true, "start": "09:00", "end": "17:00", "max_min": 480, "max_high_min": 180}, ...}, "days_off": ["2026-12-25"], "auto_rollover": true}
    work_schedule_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # Cached calibration summary derived from task_reflections
    calibration_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    tutorial_seeded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tg11_vault_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="profile")


class IdentityLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """ApplicationIdentityLink for Flowboard: local user <-> external identity."""

    __tablename__ = "identity_links"
    __table_args__ = (
        UniqueConstraint("provider", "subject", name="uq_identity_provider_subject"),
        Index("ix_identity_links_user", "user_id"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)  # "tg11" (OIDC issuer alias)
    issuer: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)  # TG11 global user UUID (`sub`)
    email_at_link: Mapped[str] = mapped_column(String(254), default="", nullable=False)
    username_at_link: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    migration_source: Mapped[str] = mapped_column(String(64), default="oidc_login", nullable=False)  # oidc_login|account_link|admin
    migration_status: Mapped[str] = mapped_column(String(32), default="linked", nullable=False)  # linked|pending|revoked
    linked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="identity_links")


class PasswordResetToken(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "password_reset_tokens"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class UserSession(UUIDPrimaryKeyMixin, Base):
    """Server-side session registry: lets the user see/revoke sessions and lets
    us invalidate cookies on password change.  The cookie stores the session id
    (signed); nothing sensitive lives in the cookie."""

    __tablename__ = "user_sessions"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    user_agent: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    ip_address: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    auth_method: Mapped[str] = mapped_column(String(32), default="password", nullable=False)  # password|tg11
