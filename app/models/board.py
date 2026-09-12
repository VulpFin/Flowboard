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
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from .common import TimestampMixin, UUIDPrimaryKeyMixin


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
