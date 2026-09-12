# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Tasks.  Field names intentionally match the pre-2.0 JSON schema so that the
legacy import is lossless (`contexts`, `estimate_min`, `importance`, `energy`,
`depends_on`, `manual_order`, `last_actual_min`, `completions`)."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .common import TimestampMixin, UUIDPrimaryKeyMixin, utcnow


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_board_done", "board_id", "done"),
        Index("ix_tasks_board_due", "board_id", "due_at"),
    )

    board_id: Mapped[str] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), nullable=False)
    created_by_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    legacy_id: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)  # pre-2.0 8-char id

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    estimate_min: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    energy: Mapped[str] = mapped_column(String(8), default="medium", nullable=False)
    due: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # "YYYY-MM-DD" or "YYYY-MM-DD HH:MM" (display form)
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # parsed form used for sorting/calendars
    contexts_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    depends_on_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)  # open|blocked|done|archived
    done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    manual_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_actual_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parent_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)  # subtasks
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scheduled_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)  # day the planner placed it on
    scheduled_start: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)  # "HH:MM" slot inside that day (energy-curve aware)
    rollover_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, default="", nullable=False)  # AI "how to do this" (markdown)
    assigned_to_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)  # board member who owns this task

    # ---- convenience accessors (keep templates/planner unchanged) -------
    @property
    def contexts(self) -> List[str]:
        try:
            return list(json.loads(self.contexts_json or "[]"))
        except Exception:
            return []

    @contexts.setter
    def contexts(self, value: List[str]) -> None:
        self.contexts_json = json.dumps([str(v)[:32] for v in (value or [])][:5])

    @property
    def depends_on(self) -> List[str]:
        try:
            return list(json.loads(self.depends_on_json or "[]"))
        except Exception:
            return []

    @depends_on.setter
    def depends_on(self, value: List[str]) -> None:
        self.depends_on_json = json.dumps([str(v) for v in (value or [])])

    @property
    def tags(self) -> List[str]:
        try:
            return list(json.loads(self.tags_json or "[]"))
        except Exception:
            return []

    @tags.setter
    def tags(self, value: List[str]) -> None:
        self.tags_json = json.dumps([str(v)[:40] for v in (value or [])][:20])

    @property
    def primary_context(self) -> str:
        c = self.contexts
        return c[0] if c else "General"

    @property
    def is_overdue(self) -> bool:
        return bool(self.due_at and not self.done and self.due_at < utcnow())

    @property
    def created(self) -> str:  # legacy template compatibility
        return self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "estimate_min": self.estimate_min,
            "importance": self.importance,
            "energy": self.energy,
            "due": self.due,
            "contexts": self.contexts,
            "depends_on": self.depends_on,
            "tags": self.tags,
            "status": self.status,
            "done": self.done,
            "manual_order": self.manual_order,
            "last_actual_min": self.last_actual_min,
            "completions": self.completions,
            "parent_id": self.parent_id,
            "scheduled_date": self.scheduled_date.isoformat() if self.scheduled_date else None,
            "scheduled_start": self.scheduled_start,
            "assigned_to_id": self.assigned_to_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TaskActivity(UUIDPrimaryKeyMixin, Base):
    """Append-only history for a board (created/updated/completed/AI applied)."""

    __tablename__ = "task_activity"
    __table_args__ = (Index("ix_activity_board_time", "board_id", "created_at"),)

    board_id: Mapped[str] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), nullable=False)
    task_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    actor_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    actor_kind: Mapped[str] = mapped_column(String(8), default="user", nullable=False)  # user|ai|system
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    data_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class TaskReflection(UUIDPrimaryKeyMixin, Base):
    """What the user tells us after finishing a task - the raw material for the
    per-user calibration profile (see services/reflections.py)."""

    __tablename__ = "task_reflections"
    __table_args__ = (Index("ix_reflection_user_time", "user_id", "created_at"),)

    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    board_id: Mapped[str] = mapped_column(String(36), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    context: Mapped[str] = mapped_column(String(32), default="General", nullable=False)
    estimate_min: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actual_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    planned_energy: Mapped[str] = mapped_column(String(8), default="medium", nullable=False)
    actual_energy: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    clarity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 1-5 were the instructions/description clear
    difficulty: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 1-5
    had_instructions: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    hour_of_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 0-23 local hour the task was finished (learned energy curve)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
