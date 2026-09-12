# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Work schedule + capacity-aware day planning.

A user's schedule (Settings → Schedule) gives every weekday: enabled,
start/end time, max minutes of work and max minutes of *high-energy* work,
plus explicit days off.  `auto_schedule` assigns `scheduled_date` to open
tasks in planner priority order without exceeding a day's capacity; anything
that does not fit spills to the next day with room.  `rollover` moves unfinished
tasks whose day has passed onto the next available day (lazily, when a board
or the schedule page is opened) so nothing silently disappears.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..models import Board, Task, User
from . import tasks as task_service
from .planner import priority_score, topo_sort_available

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DEFAULT_DAY = {"enabled": True, "start": "09:00", "end": "17:00", "max_min": 480, "max_high_min": 180}
DEFAULT_WEEKEND = {"enabled": False, "start": "10:00", "end": "14:00", "max_min": 120, "max_high_min": 60}


@dataclass
class DayCapacity:
    day: date
    enabled: bool
    start: str
    end: str
    max_min: int
    max_high_min: int
    used_min: int = 0
    used_high_min: int = 0

    @property
    def free_min(self) -> int:
        return max(0, self.max_min - self.used_min) if self.enabled else 0

    @property
    def free_high_min(self) -> int:
        return max(0, self.max_high_min - self.used_high_min) if self.enabled else 0

    def fits(self, t: Task) -> bool:
        if not self.enabled or t.estimate_min > self.free_min:
            return False
        return t.energy != "high" or t.estimate_min <= self.free_high_min

    def add(self, t: Task) -> None:
        self.used_min += t.estimate_min
        if t.energy == "high":
            self.used_high_min += t.estimate_min


def load_schedule(user: User) -> Dict:
    try:
        raw = json.loads(user.profile.work_schedule_json or "{}") if user.profile else {}
    except Exception:
        raw = {}
    days = raw.get("days") or {}
    out_days = {}
    for i in range(7):
        base = dict(DEFAULT_DAY if i < 5 else DEFAULT_WEEKEND)
        base.update(days.get(str(i)) or {})
        out_days[str(i)] = base
    return {"days": out_days, "days_off": sorted(set(raw.get("days_off") or [])), "auto_rollover": raw.get("auto_rollover", True), "horizon_days": int(raw.get("horizon_days", 28))}


def save_schedule(db: Session, user: User, form: Dict[str, str]) -> Dict:
    days = {}
    for i in range(7):
        k = str(i)
        days[k] = {
            "enabled": form.get(f"enabled_{k}") in ("on", "1", "true"),
            "start": (form.get(f"start_{k}") or DEFAULT_DAY["start"])[:5],
            "end": (form.get(f"end_{k}") or DEFAULT_DAY["end"])[:5],
            "max_min": task_service.clamp_int(form.get(f"max_min_{k}"), 0, 1440, 480),
            "max_high_min": task_service.clamp_int(form.get(f"max_high_min_{k}"), 0, 1440, 180),
        }
    days_off = []
    for tok in (form.get("days_off") or "").replace("\n", ",").split(","):
        tok = tok.strip()
        try:
            days_off.append(date.fromisoformat(tok).isoformat())
        except ValueError:
            continue
    sched = {"days": days, "days_off": sorted(set(days_off)), "auto_rollover": form.get("auto_rollover") in ("on", "1", "true"), "horizon_days": task_service.clamp_int(form.get("horizon_days"), 7, 90, 28)}
    user.profile.work_schedule_json = json.dumps(sched)
    db.flush()
    return sched


def capacity_for(user: User, day: date, sched: Optional[Dict] = None) -> DayCapacity:
    sched = sched or load_schedule(user)
    d = sched["days"][str(day.weekday())]
    enabled = bool(d["enabled"]) and day.isoformat() not in sched["days_off"]
    return DayCapacity(day=day, enabled=enabled, start=d["start"], end=d["end"], max_min=int(d["max_min"]), max_high_min=int(d["max_high_min"]))


def _today() -> date:
    return datetime.now().date()


def day_plan(db: Session, user: User, boards: List[Board], start: date, days: int = 7) -> List[Tuple[DayCapacity, List[Task]]]:
    sched = load_schedule(user)
    caps = {start + timedelta(days=i): capacity_for(user, start + timedelta(days=i), sched) for i in range(days)}
    buckets: Dict[date, List[Task]] = {d: [] for d in caps}
    for b in boards:
        for t in task_service.list_tasks(db, b, include_done=False):
            if t.scheduled_date in buckets:
                buckets[t.scheduled_date].append(t)
                caps[t.scheduled_date].add(t)
    for lst in buckets.values():
        lst.sort(key=lambda t: (-t.importance, t.manual_order))
    return [(caps[d], buckets[d]) for d in sorted(caps)]


def rollover(db: Session, user: User, board: Board, *, today: Optional[date] = None) -> int:
    """Move unfinished tasks scheduled before today to the next day with room."""
    sched = load_schedule(user)
    if not sched.get("auto_rollover", True):
        return 0
    today = today or _today()
    stale = [t for t in task_service.list_tasks(db, board, include_done=False) if t.scheduled_date and t.scheduled_date < today]
    if not stale:
        return 0
    for t in stale:
        t.scheduled_date = None
        t.rollover_count += 1
    db.flush()
    moved = auto_schedule(db, user, board, only_ids={t.id for t in stale}, today=today)
    task_service.log_activity(db, board, actor_kind="system", action="rollover", summary=f"Rolled {moved} unfinished task(s) forward to the next free day")
    return moved


def auto_schedule(db: Session, user: User, board: Board, *, only_ids: Optional[set] = None, today: Optional[date] = None, reschedule_all: bool = False) -> int:
    """Assign scheduled_date to open tasks respecting daily capacity."""
    sched = load_schedule(user)
    today = today or _today()
    horizon = sched.get("horizon_days", 28)
    tasks = task_service.tasks_by_id(db, board)
    open_tasks = {tid: t for tid, t in tasks.items() if not t.done}
    # capacity already used on each day by tasks we are NOT moving (across all the user's boards)
    from .boards import list_boards_for_user

    caps: Dict[date, DayCapacity] = {today + timedelta(days=i): capacity_for(user, today + timedelta(days=i), sched) for i in range(horizon)}
    for b in list_boards_for_user(db, user):
        for t in task_service.list_tasks(db, b, include_done=False):
            moving = (only_ids is not None and t.id in only_ids) or (only_ids is None and (reschedule_all or t.scheduled_date is None) and t.board_id == board.id)
            if t.scheduled_date in caps and not moving:
                caps[t.scheduled_date].add(t)
    candidates = [t for t in topo_sort_available(tasks) if t.id in open_tasks and ((only_ids is None and (reschedule_all or t.scheduled_date is None)) or (only_ids is not None and t.id in only_ids))]
    now = datetime.now()
    candidates.sort(key=lambda t: (t.due_at or datetime.max, -priority_score(t, now)))
    placed = 0
    for t in candidates:
        # never before dependencies
        earliest = today
        for dep in t.depends_on:
            d = tasks.get(dep)
            if d is not None and not d.done and d.scheduled_date and d.scheduled_date >= earliest:
                earliest = d.scheduled_date + timedelta(days=1)
        for day in sorted(caps):
            if day < earliest:
                continue
            if caps[day].fits(t):
                caps[day].add(t)
                t.scheduled_date = day
                placed += 1
                break
        else:
            # does not fit anywhere in the horizon (task bigger than any day) -> first enabled day, flagged by overflow
            for day in sorted(caps):
                if caps[day].enabled and day >= earliest:
                    t.scheduled_date = day
                    caps[day].add(t)
                    placed += 1
                    break
    db.flush()
    return placed


def schedule_summary_for_ai(user: User, boards_tasks: List[Task], days: int = 7) -> str:
    sched = load_schedule(user)
    today = _today()
    lines = ["Work schedule (per day: max minutes / max high-energy minutes):"]
    for i in range(days):
        d = today + timedelta(days=i)
        cap = capacity_for(user, d, sched)
        used = sum(t.estimate_min for t in boards_tasks if t.scheduled_date == d and not t.done)
        lines.append(f"- {d.isoformat()} {WEEKDAYS[d.weekday()][:3]}: {'off' if not cap.enabled else f'{cap.start}-{cap.end}, {cap.max_min} min ({cap.max_high_min} high-energy), {used} min already scheduled'}")
    return "\n".join(lines)
