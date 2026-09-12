# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Per-user local time.

Everything stored in the database is naive UTC (`models.utcnow`); everything a
user sees - the day their tasks are scheduled on, the hour a reflection was
recorded at, when the morning digest goes out - is *their* local time, taken
from `UserProfile.timezone` (an IANA name, default UTC).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

try:  # pragma: no cover - always present on 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def tz_for(user) -> timezone:
    """The user's tzinfo, falling back to UTC for unknown/absent names."""
    name = ""
    profile = getattr(user, "profile", None) if user is not None else None
    if profile is not None:
        name = (profile.timezone or "").strip()
    if not name or name.upper() == "UTC" or ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)  # type: ignore[return-value]
    except Exception:
        return timezone.utc


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def local_now(user, *, now: Optional[datetime] = None) -> datetime:
    """Timezone-aware "now" in the user's timezone."""
    base = now or now_utc()
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(tz_for(user))


def local_today(user, *, now: Optional[datetime] = None) -> date:
    return local_now(user, now=now).date()


def local_hour(user, *, now: Optional[datetime] = None) -> int:
    return local_now(user, now=now).hour


def to_local(user, dt: datetime) -> datetime:
    """Convert an aware (or naive-UTC) datetime into the user's timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz_for(user))


def minutes_of_day(value: str, default: int = 0) -> int:
    """"HH:MM" -> minutes since midnight (tolerant of junk)."""
    try:
        h, m = (value or "").split(":")[:2]
        return max(0, min(24 * 60, int(h) * 60 + int(m)))
    except Exception:
        return default


def hhmm(minutes: int) -> str:
    minutes = max(0, min(24 * 60 - 1, int(minutes)))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
