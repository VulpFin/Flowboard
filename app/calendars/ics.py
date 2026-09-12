# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""RFC 5545 iCalendar generation (no third-party dependency).

Tasks with a due date become events:
  * date-only due   -> all-day VEVENT on that day
  * date+time due   -> timed VEVENT of `estimate_min` ending at the due time
Tasks without a due date are exported as VTODO items (Apple Reminders /
Thunderbird understand them; Google Calendar ignores VTODO).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from ..models import Board, Task
from ..services.tasks import parse_due

PRODID = "-//TG11//Vulpfin Flowboard 2.0//EN"


def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Fold lines longer than 75 octets (RFC 5545 §3.1)."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 74:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def _dt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def _date(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def task_uid(task: Task) -> str:
    return f"task-{task.id}@flowboard.fyi"


def _slot_minutes(value: Optional[str]) -> Optional[int]:
    """"HH:MM" -> minutes since midnight, or None."""
    try:
        h, m = (value or "").split(":")[:2]
        h, m = int(h), int(m)
    except (ValueError, TypeError):
        return None
    return h * 60 + m if 0 <= h <= 23 and 0 <= m <= 59 else None


def task_component(task: Task, *, now: Optional[datetime] = None, board_name: str = "", default_duration_min: int = 60) -> List[str]:
    now = now or datetime.utcnow()
    due = task.due_at or parse_due(task.due)
    summary = f"[{task.primary_context}] {task.title}"
    desc_parts = [task.description or ""]
    meta = f"Estimate: {task.estimate_min} min · Importance: {task.importance}/5 · Energy: {task.energy}"
    if board_name:
        meta = f"Board: {board_name} · " + meta
    desc_parts.append(meta)
    description = "\n".join(p for p in desc_parts if p)
    if due is None and task.scheduled_date:
        # 2.2: a task with a planned slot is a real appointment even without a due date
        start_min = _slot_minutes(task.scheduled_start)
        if start_min is not None:
            due = datetime.combine(task.scheduled_date, datetime.min.time()) + timedelta(minutes=start_min + max(5, task.estimate_min or default_duration_min))
    seq = int(task.updated_at.timestamp()) if task.updated_at else 0
    common = [
        f"UID:{task_uid(task)}",
        f"DTSTAMP:{_dt(now)}Z",
        f"SUMMARY:{_esc(summary)}",
        f"DESCRIPTION:{_esc(description)}",
        f"SEQUENCE:{seq % 2147483647}",
        f"CATEGORIES:{_esc(','.join(task.contexts) or 'General')}",
        f"PRIORITY:{ {5: 1, 4: 3, 3: 5, 2: 7, 1: 9}.get(task.importance, 5)}",
    ]
    if due is None:
        lines = ["BEGIN:VTODO"] + common
        lines.append(f"STATUS:{'COMPLETED' if task.done else 'NEEDS-ACTION'}")
        if task.done and task.completed_at:
            lines.append(f"COMPLETED:{_dt(task.completed_at)}Z")
        lines.append("END:VTODO")
        return lines
    lines = ["BEGIN:VEVENT"] + common
    if due.hour == 0 and due.minute == 0 and (task.due and len(task.due) <= 10):
        lines.append(f"DTSTART;VALUE=DATE:{_date(due)}")
        lines.append(f"DTEND;VALUE=DATE:{_date(due + timedelta(days=1))}")
    else:
        duration = max(5, task.estimate_min or default_duration_min)
        lines.append(f"DTSTART:{_dt(due - timedelta(minutes=duration))}")
        lines.append(f"DTEND:{_dt(due)}")
    lines.append(f"STATUS:{'CONFIRMED' if not task.done else 'CANCELLED'}")
    lines.append("TRANSP:TRANSPARENT")
    lines.append("END:VEVENT")
    return lines


def calendar_document(components: Iterable[List[str]], *, name: str = "Flowboard", method: str = "PUBLISH", refresh_minutes: int = 30) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        f"X-WR-CALNAME:{_esc(name)}",
        f"REFRESH-INTERVAL;VALUE=DURATION:PT{refresh_minutes}M",
        f"X-PUBLISHED-TTL:PT{refresh_minutes}M",
    ]
    for comp in components:
        lines.extend(comp)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


def task_ics(task: Task, board: Board) -> str:
    return calendar_document([task_component(task, board_name=board.name)], name=board.name, method="PUBLISH")


def board_ics(tasks: List[Task], board: Board, *, include_done: bool = False, start: Optional[datetime] = None, end: Optional[datetime] = None) -> str:
    comps = []
    now = datetime.utcnow()
    for t in tasks:
        if t.done and not include_done:
            continue
        due = t.due_at
        if start and due and due < start:
            continue
        if end and due and due > end:
            continue
        comps.append(task_component(t, now=now, board_name=board.name))
    return calendar_document(comps, name=f"Flowboard · {board.name}")


def plan_ics(planned: List[Task], board: Board, *, start: Optional[datetime] = None) -> str:
    """Sequential schedule starting now (the pre-2.0 'Export ICS' behaviour)."""
    cur = start or datetime.now()
    now = datetime.utcnow()
    comps = []
    for t in planned:
        # a task the planner already gave a slot keeps it, as long as it is not in the past
        slot = _slot_minutes(t.scheduled_start)
        if t.scheduled_date and slot is not None:
            planned_at = datetime.combine(t.scheduled_date, datetime.min.time()) + timedelta(minutes=slot)
            if planned_at >= cur:
                cur = planned_at
        end = cur + timedelta(minutes=max(5, t.estimate_min))
        uid_extra = hashlib.sha1(f"{t.id}{cur.isoformat()}".encode()).hexdigest()[:8]
        comps.append([
            "BEGIN:VEVENT",
            f"UID:plan-{t.id}-{uid_extra}@flowboard.fyi",
            f"DTSTAMP:{_dt(now)}Z",
            f"DTSTART:{_dt(cur)}",
            f"DTEND:{_dt(end)}",
            f"SUMMARY:{_esc('[' + t.primary_context + '] ' + t.title)}",
            f"DESCRIPTION:{_esc(t.description or '')}",
            "END:VEVENT",
        ])
        cur = end
    return calendar_document(comps, name=f"Flowboard plan · {board.name}")


def content_fingerprint(task: Task) -> str:
    return hashlib.sha256(f"{task.title}|{task.description}|{task.due}|{task.estimate_min}|{task.done}|{task.primary_context}".encode()).hexdigest()[:32]
