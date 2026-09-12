# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Calendar busy time -> scheduling capacity.

Connected Google / Microsoft calendars are asked for the busy intervals across
the scheduling horizon; the minutes that fall inside a day's working window are
subtracted from that day's limit, so "8 hours a day" means eight hours *minus
the meetings that are already in the calendar*.

Everything here is best effort: the result is cached on the connection for
`CACHE_TTL_SEC` and any failure (no connection, expired token, provider down)
returns nothing at all rather than an error - the schedule still works, it just
does not know about meetings.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..models import CalendarConnection, User, utcnow
from ..services import clock
from .links import fresh_token, list_connections
from .providers import get_provider

CACHE_TTL_SEC = 900  # 15 minutes
Interval = Tuple[datetime, datetime]


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    """RFC3339 / Graph timestamp -> aware UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    if "." in s:  # Graph returns 7 fractional digits; fromisoformat wants <= 6
        head, _, tail = s.partition(".")
        digits = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                digits += ch
            else:
                rest = tail[i:]
                break
        s = f"{head}.{digits[:6]}{rest}" if digits else head + rest
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def merge_intervals(intervals: List[Interval]) -> List[Interval]:
    out: List[Interval] = []
    for s, e in sorted(i for i in intervals if i[1] > i[0]):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


# --------------------------------------------------------------------------

def _cached(conn: CalendarConnection, start: datetime, end: datetime) -> Optional[List[Interval]]:
    if conn.busy_fetched_at is None:
        return None
    age = (utcnow() - conn.busy_fetched_at).total_seconds()
    if age < 0 or age > CACHE_TTL_SEC:
        return None
    try:
        cache = json.loads(conn.busy_cache_json or "{}")
    except Exception:
        return None
    c_from, c_to = parse_dt(cache.get("from")), parse_dt(cache.get("to"))
    if not c_from or not c_to or c_from > start or c_to < end:
        return None
    out: List[Interval] = []
    for pair in cache.get("intervals") or []:
        s, e = parse_dt(pair[0] if len(pair) > 0 else None), parse_dt(pair[1] if len(pair) > 1 else None)
        if s and e:
            out.append((s, e))
    return out


def connection_intervals(db: Session, conn: CalendarConnection, start: datetime, end: datetime) -> List[Interval]:
    """Busy intervals for one connection, cached for 15 minutes."""
    cached = _cached(conn, start, end)
    if cached is not None:
        return cached
    provider = get_provider(conn.provider)
    token = fresh_token(db, conn, provider)
    intervals = provider.free_busy(token, conn.target_calendar_id, start, end)
    intervals = merge_intervals([(s, e) for s, e in intervals if s and e])
    conn.busy_cache_json = json.dumps({
        "from": start.isoformat(), "to": end.isoformat(),
        "intervals": [[s.isoformat(), e.isoformat()] for s, e in intervals],
    })
    conn.busy_fetched_at = utcnow()
    db.flush()
    return intervals


def busy_windows(db: Session, user: User, start: date, days: int, sched: Dict) -> Dict[date, Dict]:
    """{day: {"minutes": int, "slots": [(from_min, to_min)], "count": int}}.

    Minutes are counted only inside that weekday's working window, so an
    evening event does not eat into a 09:00-17:00 day.
    """
    tz = clock.tz_for(user)
    window_start = datetime.combine(start, time.min).replace(tzinfo=tz).astimezone(timezone.utc)
    window_end = datetime.combine(start + timedelta(days=days), time.min).replace(tzinfo=tz).astimezone(timezone.utc)

    intervals: List[Interval] = []
    for conn in list_connections(db, user):
        if not conn.enabled or not conn.busy_enabled or conn.status == "revoked":
            continue
        try:
            intervals.extend(connection_intervals(db, conn, window_start, window_end))
        except Exception:
            continue  # one broken calendar must not break scheduling
    if not intervals:
        return {}
    merged = merge_intervals(intervals)

    out: Dict[date, Dict] = {}
    for i in range(days):
        day = start + timedelta(days=i)
        cfg = sched["days"][str(day.weekday())]
        win_s = clock.minutes_of_day(cfg.get("start") or "09:00", 9 * 60)
        win_e = clock.minutes_of_day(cfg.get("end") or "17:00", 17 * 60)
        if win_e <= win_s:
            continue
        day_start = datetime.combine(day, time.min).replace(tzinfo=tz)
        slots: List[Tuple[int, int]] = []
        for s, e in merged:
            s_min = int((s.astimezone(tz) - day_start).total_seconds() // 60)
            e_min = int((e.astimezone(tz) - day_start).total_seconds() // 60)
            s_min, e_min = max(s_min, win_s), min(e_min, win_e)
            if e_min > s_min:
                slots.append((s_min, e_min))
        if slots:
            out[day] = {"minutes": sum(e - s for s, e in slots), "slots": slots, "count": len(slots)}
    return out
