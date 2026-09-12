# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Boards (workspaces) and membership.

Every board-scoped entity (tasks, activity, AI change sets, calendar links)
carries a `board_id`.  Access is always resolved through `BoardMembership`;
the owner always has an implicit `owner` membership row so that future shared
boards need no schema change.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from .common import TimestampMixin, UUIDPrimaryKeyMixin, utcnow


class BoardRole(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"

    @property
    def can_edit(self) -> bool:
        return self in (BoardRole.OWNER, BoardRole.ADMIN, BoardRole.EDITOR)

    @property
    def can_manage(self) -> bool:
        return self in (BoardRole.OWNER, BoardRole.ADMIN)


class Board(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "boards"
    __table_args__ = (UniqueConstraint("owner_id", "slug", name="uq_board_owner_slug"),)

    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    icon: Mapped[str] = mapped_column(String(32), default="", nullable=False)  # emoji or icon name
    color: Mapped[str] = mapped_column(String(16), default="", nullable=False)  # hex colour
    theme_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Board-level settings (JSON): ai_model override, default context, etc.
    settings_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    position: Mapped[int] = mapped_column(default=0, nullable=False)

    memberships: Mapped[list["BoardMembership"]] = relationship(back_populates="board", cascade="all, delete-orphan")
    #: the owning user - lets any code that holds a Board build its public
    #: reference ("<owner>~<slug>") without another query
    owner: Mapped["User"] = relationship("User", lazy="selectin")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Board {self.slug} owner={self.owner_id}>"


class BoardMembership(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "board_memberships"
    __table_args__ = (
        UniqueConstraint("board_id", "user_id", name="uq_membership_board_user"),
        Index("ix_membership_user", "user_id"),
    )

    board_id: Mapped[str] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default=BoardRole.OWNER.value, nullable=False)
    invited_by_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    accepted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    board: Mapped["Board"] = relationship(back_populates="memberships")

    @property
    def role_enum(self) -> BoardRole:
        return BoardRole(self.role)


class BoardInvite(UUIDPrimaryKeyMixin, Base):
    """A pending invitation to join a board.

    Invitations are addressed to an *email*, not a user, so you can invite
    someone who has not signed up yet: they register with that address and the
    invitation is waiting for them at /invites.  The emailed link carries a
    256-bit token; only its hash is stored, exactly like password-reset and
    calendar-feed tokens.  Membership rows are created on acceptance, so
    "member" continues to mean "someone who said yes".
    """

    __tablename__ = "board_invites"
    __table_args__ = (Index("ix_board_invites_email", "email"), Index("ix_board_invites_board", "board_id"))

    board_id: Mapped[str] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default=BoardRole.EDITOR.value, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(12), default="", nullable=False)
    invited_by_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    message: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    accepted_by_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    declined_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    board: Mapped["Board"] = relationship("Board", lazy="selectin")

    @property
    def is_open(self) -> bool:
        return not (self.accepted_at or self.declined_at or self.revoked_at) and self.expires_at > utcnow()

    @property
    def state(self) -> str:
        if self.accepted_at:
            return "accepted"
        if self.declined_at:
            return "declined"
        if self.revoked_at:
            return "revoked"
        return "pending" if self.expires_at > utcnow() else "expired"
