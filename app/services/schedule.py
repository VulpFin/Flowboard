# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Work schedule + capacity-aware day planning.

A user's schedule (Settings → Schedule) gives every weekday: enabled,
start/end time, max minutes of work and max minutes of *high-energy* work,
plus explicit days off.  `auto_schedule` assigns `scheduled_date` (and, since
2.2, a `scheduled_start` slot) to open tasks in planner priority order without
exceeding a day's capacity; anything that does not fit spills to the next day
with room.  `rollover` moves unfinished tasks whose day has passed onto the
next available day (lazily, when a board or the schedule page is opened) so
nothing silently disappears.

2.2 adds three things on top of that:

* **Calendar-aware capacity** - busy intervals from connected Google/Microsoft
  calendars are subtracted from each day's limit (`DayCapacity.busy_min`).
* **An energy curve** - each day is divided into high / medium / low energy
  blocks; high-energy tasks are placed inside high-energy blocks when one is
  free.  The declared curve can be replaced by one learned from reflections.
* **Per-assignee capacity** - a task's effective owner is its assignee, or the
  board owner when unassigned; each owner is scheduled against their own
  schedule, calendar and existing workload.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..models import Board, Task, User
from . import clock
from . import tasks as task_service
from .planner import priority_score, topo_sort_available

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DEFAULT_DAY = {"enabled": True, "start": "09:00", "end": "17:00", "max_min": 480, "max_high_min": 180}
DEFAULT_WEEKEND = {"enabled": False, "start": "10:00", "end": "14:00", "max_min": 120, "max_high_min": 60}
ENERGY_LEVELS = ("high", "medium", "low")
#: granularity of the intra-day placement search
SLOT_STEP = 15


# --------------------------------------------------------------------------
# schedule document
# --------------------------------------------------------------------------

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
        base.setdefault("high_end", "")
        base.setdefault("medium_end", "")
        out_days[str(i)] = base
    return {
        "days": out_days,
        "days_off": sorted(set(raw.get("days_off") or [])),
        "auto_rollover": raw.get("auto_rollover", True),
        "horizon_days": int(raw.get("horizon_days", 28)),
        "always_full_reflection": bool(raw.get("always_full_reflection", False)),
        "use_learned_curve": bool(raw.get("use_learned_curve", False)),
        "calendar_busy": bool(raw.get("calendar_busy", True)),
    }


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
            "high_end": (form.get(f"high_end_{k}") or "")[:5],
            "medium_end": (form.get(f"medium_end_{k}") or "")[:5],
        }
    days_off = []
    for tok in (form.get("days_off") or "").replace("\n", ",").split(","):
        tok = tok.strip()
        try:
            days_off.append(date.fromisoformat(tok).isoformat())
        except ValueError:
            continue
    sched = {
        "days": days,
        "days_off": sorted(set(days_off)),
        "auto_rollover": form.get("auto_rollover") in ("on", "1", "true"),
        "horizon_days": task_service.clamp_int(form.get("horizon_days"), 7, 90, 28),
        "always_full_reflection": form.get("always_full_reflection") in ("on", "1", "true"),
        "use_learned_curve": form.get("use_learned_curve") in ("on", "1", "true"),
        "calendar_busy": form.get("calendar_busy") in ("on", "1", "true"),
    }
    user.profile.work_schedule_json = json.dumps(sched)
    db.flush()
    return sched


# --------------------------------------------------------------------------
# energy curve
# --------------------------------------------------------------------------

def declared_blocks(day_cfg: Dict) -> List[Tuple[int, int, str]]:
    """[(start_minute, end_minute, level)] for one weekday's declared curve.

    Two boundaries describe the whole day: work starts high-energy until
    `high_end`, is medium until `medium_end`, and low after that.  Unset
    boundaries default to a morning-high / afternoon-medium / evening-low
    split of the day's own window.
    """
    start = clock.minutes_of_day(day_cfg.get("start") or DEFAULT_DAY["start"], 9 * 60)
    end = clock.minutes_of_day(day_cfg.get("end") or DEFAULT_DAY["end"], 17 * 60)
    if end <= start:
        end = min(24 * 60, start + 60)
    span = end - start
    high_end = clock.minutes_of_day(day_cfg.get("high_end") or "", 0) or (start + max(30, min(3 * 60, span // 2)))
    medium_end = clock.minutes_of_day(day_cfg.get("medium_end") or "", 0) or (start + max(45, min(6 * 60, (span * 3) // 4)))
    high_end = max(start, min(high_end, end))
    medium_end = max(high_end, min(medium_end, end))
    blocks = [(start, high_end, "high"), (high_end, medium_end, "medium"), (medium_end, end, "low")]
    return [b for b in blocks if b[1] > b[0]]


def learned_blocks(levels: Dict[str, str], day_cfg: Dict) -> List[Tuple[int, int, str]]:
    """Turn a learned per-hour curve into blocks clipped to the day's window."""
    start = clock.minutes_of_day(day_cfg.get("start") or DEFAULT_DAY["start"], 9 * 60)
    end = clock.minutes_of_day(day_cfg.get("end") or DEFAULT_DAY["end"], 17 * 60)
    out: List[Tuple[int, int, str]] = []
    for hour in range(24):
        lvl = levels.get(str(hour))
        if not lvl:
            continue
        s, e = max(start, hour * 60), min(end, (hour + 1) * 60)
        if e <= s:
            continue
        if out and out[-1][2] == lvl and out[-1][1] == s:
            out[-1] = (out[-1][0], e, lvl)
        else:
            out.append((s, e, lvl))
    return out


def energy_blocks(user: User, day: date, sched: Optional[Dict] = None, learned: Optional[Dict] = None) -> List[Tuple[int, int, str]]:
    sched = sched or load_schedule(user)
    day_cfg = sched["days"][str(day.weekday())]
    if sched.get("use_learned_curve"):
        if learned is None:
            from . import reflections as reflection_service

            learned = reflection_service.learned_curve(user)
        blocks = learned_blocks((learned or {}).get("levels") or {}, day_cfg)
        if blocks:
            return blocks
    return declared_blocks(day_cfg)


def level_at(blocks: List[Tuple[int, int, str]], minute: int) -> str:
    for s, e, lvl in blocks:
        if s <= minute < e:
            return lvl
    return "low"


def curve_text(blocks: List[Tuple[int, int, str]]) -> str:
    return ", ".join(f"{clock.hhmm(s)}-{clock.hhmm(e)} {lvl}" for s, e, lvl in blocks)


# --------------------------------------------------------------------------
# day capacity
# --------------------------------------------------------------------------

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
    busy_min: int = 0  # minutes taken by external calendar events inside the window
    blocks: List[Tuple[int, int, str]] = field(default_factory=list)
    busy_slots: List[Tuple[int, int]] = field(default_factory=list)
    taken: List[Tuple[int, int]] = field(default_factory=list)  # minutes already assigned to tasks

    # ---- limits ----
    @property
    def available_min(self) -> int:
        """The day's limit after meetings are subtracted."""
        return max(0, self.max_min - self.busy_min)

    @property
    def free_min(self) -> int:
        return max(0, self.available_min - self.used_min) if self.enabled else 0

    @property
    def free_high_min(self) -> int:
        return max(0, self.max_high_min - self.used_high_min) if self.enabled else 0

    @property
    def over(self) -> bool:
        return self.enabled and self.used_min > self.available_min

    def fits(self, t: Task) -> bool:
        if not self.enabled or t.estimate_min > self.free_min:
            return False
        return t.energy != "high" or t.estimate_min <= self.free_high_min

    # ---- intra-day slots ----
    def _window(self) -> Tuple[int, int]:
        return clock.minutes_of_day(self.start, 9 * 60), clock.minutes_of_day(self.end, 17 * 60)

    def _is_free(self, s: int, e: int) -> bool:
        for bs, be in self.busy_slots + self.taken:
            if s < be and bs < e:
                return False
        return True

    def find_slot(self, duration: int, levels: Optional[Tuple[str, ...]] = None) -> Optional[int]:
        """Earliest start minute where `duration` minutes are free (and, if
        `levels` is given, entirely inside blocks of those energy levels)."""
        win_s, win_e = self._window()
        duration = max(1, duration)
        step = SLOT_STEP
        s = win_s
        while s + duration <= win_e:
            if self._is_free(s, s + duration) and (
                levels is None or all(level_at(self.blocks, m) in levels for m in range(s, s + duration, step))
            ):
                return s
            s += step
        return None

    def reserve(self, start_min: int, duration: int) -> None:
        self.taken.append((start_min, start_min + max(1, duration)))
        self.taken.sort()

    def add(self, t: Task, *, slot: Optional[int] = None) -> Optional[int]:
        """Count a task against this day and reserve its minutes."""
        self.used_min += t.estimate_min
        if t.energy == "high":
            self.used_high_min += t.estimate_min
        if slot is None:
            slot = clock.minutes_of_day(t.scheduled_start, -1) if t.scheduled_start else -1
            if slot < 0:
                slot = self.find_slot(t.estimate_min)
        if slot is not None and slot >= 0:
            self.reserve(slot, t.estimate_min)
            return slot
        return None

    def place(self, t: Task) -> Optional[int]:
        """Find the best slot for a task given its energy need."""
        # each level tries its own blocks first so that demanding work keeps the
        # high-energy part of the day to itself
        if t.energy == "high":
            order: List[Optional[Tuple[str, ...]]] = [("high",), ("high", "medium"), None]
        elif t.energy == "medium":
            order = [("medium",), ("medium", "low"), None]
        else:
            order = [("low",), ("low", "medium"), None]
        for levels in order:
            slot = self.find_slot(t.estimate_min, levels)
            if slot is not None:
                return slot
        return None


def capacity_for(user: User, day: date, sched: Optional[Dict] = None, *, busy: Optional[Dict[date, Dict]] = None, learned: Optional[Dict] = None) -> DayCapacity:
    sched = sched or load_schedule(user)
    d = sched["days"][str(day.weekday())]
    enabled = bool(d["enabled"]) and day.isoformat() not in sched["days_off"]
    cap = DayCapacity(
        day=day, enabled=enabled, start=d["start"], end=d["end"],
        max_min=int(d["max_min"]), max_high_min=int(d["max_high_min"]),
        blocks=energy_blocks(user, day, sched, learned),
    )
    info = (busy or {}).get(day)
    if info and sched.get("calendar_busy", True):
        cap.busy_min = int(info.get("minutes") or 0)
        cap.busy_slots = [tuple(x) for x in (info.get("slots") or [])]
    return cap


def _today() -> date:
    return datetime.now().date()


# --------------------------------------------------------------------------
# calendar busy time (degrades silently)
# --------------------------------------------------------------------------

def busy_map(db: Session, user: User, start: date, days: int, sched: Optional[Dict] = None) -> Dict[date, Dict]:
    """{day: {"minutes": int, "slots": [(from_min, to_min), ...]}} from connected
    calendars.  Any failure (no connection, expired token, network) yields {}."""
    sched = sched or load_schedule(user)
    if not sched.get("calendar_busy", True):
        return {}
    try:
        from ..calendars import busy as busy_service

        return busy_service.busy_windows(db, user, start, days, sched)
    except Exception:
        return {}


# --------------------------------------------------------------------------
# ownership
# --------------------------------------------------------------------------

def effective_owner_id(task: Task, board: Board) -> str:
    """Whose day does this task consume?  Its assignee, else the board owner."""
    return task.assigned_to_id or board.owner_id


def _owner_tasks(db: Session, owner: User) -> List[Tuple[Task, Board]]:
    """Every open task on any of the owner's boards that counts against them."""
    from .boards import list_boards_for_user

    out: List[Tuple[Task, Board]] = []
    for b in list_boards_for_user(db, owner):
        for t in task_service.list_tasks(db, b, include_done=False):
            if effective_owner_id(t, b) == owner.id:
                out.append((t, b))
    return out


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def day_plan(db: Session, user: User, boards: List[Board], start: date, days: int = 7) -> List[Tuple[DayCapacity, List[Task]]]:
    sched = load_schedule(user)
    busy = busy_map(db, user, start, days, sched)
    caps = {start + timedelta(days=i): capacity_for(user, start + timedelta(days=i), sched, busy=busy) for i in range(days)}
    buckets: Dict[date, List[Task]] = {d: [] for d in caps}
    for b in boards:
        for t in task_service.list_tasks(db, b, include_done=False):
            if t.scheduled_date in buckets and effective_owner_id(t, b) == user.id:
                buckets[t.scheduled_date].append(t)
                caps[t.scheduled_date].add(t)
    for lst in buckets.values():
        lst.sort(key=lambda t: (clock.minutes_of_day(t.scheduled_start, 24 * 60) if t.scheduled_start else 24 * 60, -t.importance, t.manual_order))
    return [(caps[d], buckets[d]) for d in sorted(caps)]


def free_minutes_by_day(db: Session, owner: User, start: date, days: int = 7) -> List[Dict]:
    """Per-day free capacity for one person - minutes only, no task detail.

    This is what the "who has room this week" panel on a shared board shows, so
    it deliberately exposes nothing about the other boards those minutes came
    from.
    """
    sched = load_schedule(owner)
    busy = busy_map(db, owner, start, days, sched)
    caps = {start + timedelta(days=i): capacity_for(owner, start + timedelta(days=i), sched, busy=busy) for i in range(days)}
    for t, b in _owner_tasks(db, owner):
        if t.scheduled_date in caps:
            caps[t.scheduled_date].add(t)
    out = []
    for d in sorted(caps):
        cap = caps[d]
        out.append({
            "day": d, "enabled": cap.enabled, "free_min": cap.free_min, "used_min": cap.used_min,
            "max_min": cap.max_min, "available_min": cap.available_min, "busy_min": cap.busy_min,
            "free_high_min": cap.free_high_min,
        })
    return out


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
        t.scheduled_start = None
        t.rollover_count += 1
    db.flush()
    moved = auto_schedule(db, user, board, only_ids={t.id for t in stale}, today=today)
    task_service.log_activity(db, board, actor_kind="system", action="rollover", summary=f"Rolled {moved} unfinished task(s) forward to the next free day")
    return moved


def _caps_for_owner(db: Session, owner: User, today: date, horizon: int, moving_ids: set) -> Dict[date, DayCapacity]:
    sched = load_schedule(owner)
    busy = busy_map(db, owner, today, horizon, sched)
    learned = None
    if sched.get("use_learned_curve"):
        from . import reflections as reflection_service

        learned = reflection_service.learned_curve(owner)
    caps = {today + timedelta(days=i): capacity_for(owner, today + timedelta(days=i), sched, busy=busy, learned=learned) for i in range(horizon)}
    for t, _b in _owner_tasks(db, owner):
        if t.scheduled_date in caps and t.id not in moving_ids:
            caps[t.scheduled_date].add(t)
    return caps


def auto_schedule(db: Session, user: User, board: Board, *, only_ids: Optional[set] = None, today: Optional[date] = None, reschedule_all: bool = False) -> int:
    """Assign `scheduled_date` (+ `scheduled_start`) to open tasks respecting the
    daily capacity, the energy curve and calendar busy time of whoever the task
    belongs to (its assignee, or the board owner)."""
    sched = load_schedule(user)
    today = today or _today()
    horizon = sched.get("horizon_days", 28)
    tasks = task_service.tasks_by_id(db, board)
    open_tasks = {tid: t for tid, t in tasks.items() if not t.done}

    def is_moving(t: Task) -> bool:
        if only_ids is not None:
            return t.id in only_ids
        return reschedule_all or t.scheduled_date is None

    candidates = [t for t in topo_sort_available(tasks) if t.id in open_tasks and is_moving(t)]
    if not candidates:
        return 0
    moving_ids = {t.id for t in candidates}

    owners: Dict[str, User] = {}
    caps_by_owner: Dict[str, Dict[date, DayCapacity]] = {}

    def caps_for(owner_id: str) -> Dict[date, DayCapacity]:
        if owner_id not in caps_by_owner:
            owner = owners.get(owner_id) or db.get(User, owner_id) or user
            owners[owner_id] = owner
            caps_by_owner[owner_id] = _caps_for_owner(db, owner, today, horizon, moving_ids)
        return caps_by_owner[owner_id]

    now = datetime.now()
    candidates.sort(key=lambda t: (t.due_at or datetime.max, -priority_score(t, now)))
    placed = 0
    for t in candidates:
        caps = caps_for(effective_owner_id(t, board))
        # never before dependencies
        earliest = today
        for dep in t.depends_on:
            d = tasks.get(dep)
            if d is not None and not d.done and d.scheduled_date and d.scheduled_date >= earliest:
                earliest = d.scheduled_date + timedelta(days=1)
        for day in sorted(caps):
            if day < earliest:
                continue
            cap = caps[day]
            if cap.fits(t):
                slot = cap.place(t)
                cap.add(t, slot=slot)
                t.scheduled_date = day
                t.scheduled_start = clock.hhmm(slot) if slot is not None else None
                placed += 1
                break
        else:
            # does not fit anywhere in the horizon (task bigger than any day) ->
            # first enabled day, visibly over capacity rather than lost
            for day in sorted(caps):
                if caps[day].enabled and day >= earliest:
                    slot = caps[day].place(t)
                    caps[day].add(t, slot=slot)
                    t.scheduled_date = day
                    t.scheduled_start = clock.hhmm(slot) if slot is not None else None
                    placed += 1
                    break
    db.flush()
    return placed


# --------------------------------------------------------------------------
# AI context
# --------------------------------------------------------------------------

def schedule_summary_for_ai(user: User, boards_tasks: List[Task], days: int = 7, *, db: Optional[Session] = None, board: Optional[Board] = None) -> str:
    sched = load_schedule(user)
    today = _today()
    busy = busy_map(db, user, today, days, sched) if db is not None else {}
    lines = ["Work schedule (per day: max minutes / max high-energy minutes):"]
    for i in range(days):
        d = today + timedelta(days=i)
        cap = capacity_for(user, d, sched, busy=busy)
        used = sum(t.estimate_min for t in boards_tasks if t.scheduled_date == d and not t.done)
        if not cap.enabled:
            lines.append(f"- {d.isoformat()} {WEEKDAYS[d.weekday()][:3]}: off")
            continue
        meetings = f", {cap.busy_min} min of meetings already in the calendar" if cap.busy_min else ""
        lines.append(
            f"- {d.isoformat()} {WEEKDAYS[d.weekday()][:3]}: {cap.start}-{cap.end}, {cap.max_min} min "
            f"({cap.max_high_min} high-energy){meetings}, {used} min already scheduled, {max(0, cap.available_min - used)} min free"
        )
    blocks = energy_blocks(user, today, sched)
    if blocks:
        source = "learned from reflections" if sched.get("use_learned_curve") else "declared"
        lines.append(f"Energy curve ({source}, today): {curve_text(blocks)}. Put high-energy tasks in high-energy blocks; `scheduled_start` is HH:MM.")
    if db is not None and board is not None:
        team = team_summary_for_ai(db, board, today, days)
        if team:
            lines.append(team)
    return "\n".join(lines)


def team_summary_for_ai(db: Session, board: Board, start: date, days: int = 7) -> str:
    """Member names + free minutes per day for a shared board (no task titles)."""
    members = list(board.memberships or [])
    if len(members) < 2:
        return ""
    out = []
    for m in members:
        member = db.get(User, m.user_id)
        if member is None:
            continue
        rows = free_minutes_by_day(db, member, start, days)
        summary = ", ".join(f"{r['day'].isoformat()} {r['free_min']}m" for r in rows if r["enabled"])
        out.append(f"- {member.display_name or member.username} ({m.role}), id {member.id}: {summary or 'no working days this week'}")
    if not out:
        return ""
    return ("Board members and their free minutes per day (from their own work schedules and calendars; "
            "assign work with `assigned_to_id` using the ids below, and never schedule someone past their free minutes):\n" + "\n".join(out))
