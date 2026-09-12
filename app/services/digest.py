# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Morning digest.

Opt-in, per user: at a chosen local hour Flowboard sends "here is your day" -
what is scheduled, what is overdue, how much capacity is left after meetings,
and the one task to start with.  When the user has their own AI provider
configured the same *Daily briefing* prompt the assistant uses is run through
it; otherwise a plain, non-AI digest is built from the same data, so the
feature never depends on an API key.

Delivery goes through `CHANNELS`, a name -> callable registry.  Email is the
only channel today; a web-push channel is a second entry plus a checkbox.

Scheduling: `python -m app.cli send-digests` is run every 15 minutes by a
systemd timer.  A user is due when their *local* time has just passed their
chosen hour and they have not been sent one today (`last_sent_on`).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Board, Task, User, UserProfile
from . import clock
from . import mail as mail_service
from . import schedule as schedule_service
from . import tasks as task_service
from .boards import list_boards_for_user
from .planner import priority_score

log = logging.getLogger("flowboard.digest")

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "hour": 7,              # local hour of day
    "boards": [],           # board ids; empty = all of them
    "only_if_due": False,   # skip days with nothing scheduled/due/overdue
    "channels": ["email"],
    "last_sent_on": None,   # ISO date of the last successful send (user-local)
}
#: how long after the chosen hour a digest may still go out (a timer that was
#: down all night should not deliver a "morning" briefing at 23:00)
CATCH_UP_MINUTES = 180


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def load(user: User) -> Dict[str, Any]:
    raw: Dict[str, Any] = {}
    profile: Optional[UserProfile] = user.profile
    if profile is not None:
        try:
            raw = json.loads(profile.digest_json or "{}") or {}
        except Exception:
            raw = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in raw.items() if k in DEFAULTS})
    out["hour"] = task_service.clamp_int(out.get("hour"), 0, 23, 7)
    out["boards"] = [str(b) for b in (out.get("boards") or [])][:50]
    out["channels"] = [c for c in (out.get("channels") or []) if c in CHANNELS] or ["email"]
    return out


def save(db: Session, user: User, form: Dict[str, str], *, all_board_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    current = load(user)
    boards = [b for b in form.getlist("boards")] if hasattr(form, "getlist") else (form.get("boards") or [])
    if isinstance(boards, str):
        boards = [boards]
    if all_board_ids is not None:
        boards = [b for b in boards if b in all_board_ids]
    data = {
        "enabled": form.get("enabled") in ("on", "1", "true"),
        "hour": task_service.clamp_int(form.get("hour"), 0, 23, current["hour"]),
        "boards": boards,
        "only_if_due": form.get("only_if_due") in ("on", "1", "true"),
        "channels": [c for c in CHANNELS if form.get(f"channel_{c}") in ("on", "1", "true")] or ["email"],
        "last_sent_on": current.get("last_sent_on"),
    }
    user.profile.digest_json = json.dumps(data)
    db.flush()
    return data


def mark_sent(db: Session, user: User, day: date) -> None:
    data = load(user)
    data["last_sent_on"] = day.isoformat()
    user.profile.digest_json = json.dumps(data)
    db.flush()


def is_due(user: User, *, now: Optional[datetime] = None, force: bool = False) -> bool:
    cfg = load(user)
    if not cfg["enabled"] and not force:
        return False
    local = clock.local_now(user, now=now)
    if force:
        return True
    if cfg.get("last_sent_on") == local.date().isoformat():
        return False
    minutes_past = (local.hour * 60 + local.minute) - cfg["hour"] * 60
    return 0 <= minutes_past <= CATCH_UP_MINUTES


# --------------------------------------------------------------------------
# content
# --------------------------------------------------------------------------

def _boards_for(db: Session, user: User, cfg: Dict[str, Any]) -> List[Board]:
    boards = list_boards_for_user(db, user)
    chosen = set(cfg.get("boards") or [])
    return [b for b in boards if not chosen or b.id in chosen]


def gather(db: Session, user: User, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Everything the digest talks about, for both the AI and the plain path."""
    cfg = load(user)
    today = clock.local_today(user, now=now)
    boards = _boards_for(db, user, cfg)
    scheduled: List[Tuple[Task, Board]] = []
    overdue: List[Tuple[Task, Board]] = []
    due_today: List[Tuple[Task, Board]] = []
    open_tasks: List[Tuple[Task, Board]] = []
    for b in boards:
        for t in task_service.list_tasks(db, b, include_done=False):
            if schedule_service.effective_owner_id(t, b) != user.id:
                continue
            open_tasks.append((t, b))
            if t.scheduled_date == today:
                scheduled.append((t, b))
            if t.is_overdue:
                overdue.append((t, b))
            elif t.due_at is not None and t.due_at.date() == today:
                due_today.append((t, b))
    scheduled.sort(key=lambda tb: (clock.minutes_of_day(tb[0].scheduled_start, 24 * 60) if tb[0].scheduled_start else 24 * 60, -tb[0].importance))

    sched_doc = schedule_service.load_schedule(user)
    busy = schedule_service.busy_map(db, user, today, 1, sched_doc)
    cap = schedule_service.capacity_for(user, today, sched_doc, busy=busy)
    for t, _b in scheduled:
        cap.add(t)

    now_dt = datetime.now()
    pool = [tb for tb in scheduled if not tb[0].done] or [tb for tb in open_tasks if tb[0].status != "blocked"]
    top = max(pool, key=lambda tb: priority_score(tb[0], now_dt)) if pool else None
    return {
        "date": today,
        "weekday": schedule_service.WEEKDAYS[today.weekday()],
        "boards": boards,
        "scheduled": scheduled,
        "overdue": overdue,
        "due_today": due_today,
        "open_count": len(open_tasks),
        "capacity": cap,
        "top": top,
        "config": cfg,
        "has_content": bool(scheduled or overdue or due_today),
    }


def _line(t: Task, b: Board, *, with_board: bool) -> str:
    bits = [f"{t.scheduled_start} " if t.scheduled_start else "", t.title, f" ({t.estimate_min} min"]
    bits.append(f", {t.energy} energy")
    if t.due:
        bits.append(f", due {t.due}")
    bits.append(")")
    if with_board:
        bits.append(f" — {b.name}")
    return "".join(bits)


def plain_text(data: Dict[str, Any]) -> str:
    cap = data["capacity"]
    many = len(data["boards"]) > 1
    out = [f"{data['weekday']} {data['date'].isoformat()}", ""]
    if data["scheduled"]:
        out.append(f"Scheduled today ({len(data['scheduled'])}):")
        out += [f"  - {_line(t, b, with_board=many)}" for t, b in data["scheduled"]]
    else:
        out.append("Nothing is scheduled for today.")
    if data["due_today"]:
        out += ["", f"Due today ({len(data['due_today'])}):"] + [f"  - {_line(t, b, with_board=many)}" for t, b in data["due_today"]]
    if data["overdue"]:
        out += ["", f"Overdue ({len(data['overdue'])}):"] + [f"  - {_line(t, b, with_board=many)}" for t, b in data["overdue"]]
    out.append("")
    if cap.enabled:
        meetings = f" ({cap.busy_min} min of that is already meetings)" if cap.busy_min else ""
        out.append(f"Capacity: {cap.used_min} of {cap.available_min} min planned, {cap.free_min} min free{meetings}.")
    else:
        out.append("Today is a day off in your work schedule.")
    if data["top"]:
        t, b = data["top"]
        out.append(f"Start with: {t.title} ({t.estimate_min} min, {t.energy} energy){' — ' + b.name if many else ''}.")
    out += ["", settings.absolute_url("/")]
    return "\n".join(out)


def ai_text(db: Session, user: User, data: Dict[str, Any]) -> Optional[str]:
    """The assistant's *Daily briefing* prompt, run with the user's own provider."""
    try:
        from ..ai.assistant import QUICK_PROMPTS, build_messages
        from ..ai.client import AIClient

        board = data["boards"][0] if data["boards"] else None
        if board is None:
            return None
        client = AIClient(db, user, board)
        if client.resolved is None:
            return None
        prompt = dict(QUICK_PROMPTS)["Daily briefing"]
        tasks = [t for t, _b in data["scheduled"]] + [t for t, _b in data["overdue"]]
        for b in data["boards"]:
            for t in task_service.list_tasks(db, b, include_done=False):
                if t not in tasks:
                    tasks.append(t)
        msgs = build_messages(board, tasks[:200], prompt + "\n\nWrite it as a short email I can read on my phone: a couple of sentences, then a short list. Do not propose any changes.", user=user, db=db)
        res = client.chat(msgs, operation="digest", max_tokens=700)
        text = (res.content or "").strip()
        return text or None
    except Exception as exc:  # any provider problem -> plain digest
        log.info("digest: AI briefing unavailable for %s (%s)", user.email, exc)
        return None


def build(db: Session, user: User, *, now: Optional[datetime] = None, use_ai: bool = True) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """(subject, body, data) - or None when there is nothing worth sending."""
    data = gather(db, user, now=now)
    if data["config"]["only_if_due"] and not data["has_content"]:
        return None
    body = (ai_text(db, user, data) if use_ai else None)
    source = "assistant"
    if not body:
        body, source = plain_text(data), "plain"
    else:
        body = body + "\n\n" + plain_text(data)
    counts = []
    if data["scheduled"]:
        counts.append(f"{len(data['scheduled'])} scheduled")
    if data["overdue"]:
        counts.append(f"{len(data['overdue'])} overdue")
    subject = f"{settings.FLOWBOARD_SITE_NAME}: {data['weekday']} — " + (", ".join(counts) if counts else "nothing scheduled")
    data["source"] = source
    return subject, body, data


# --------------------------------------------------------------------------
# delivery channels
# --------------------------------------------------------------------------

def _channel_email(user: User, subject: str, body: str) -> bool:
    return mail_service.send_mail(user.email, subject, body)


#: name -> sender.  Add e.g. "push" here (plus a checkbox in the settings form
#: and an entry in DEFAULTS["channels"]) to gain a second delivery channel.
CHANNELS: Dict[str, Callable[[User, str, str], bool]] = {"email": _channel_email}


def deliver(user: User, subject: str, body: str, channels: List[str]) -> Dict[str, bool]:
    out: Dict[str, bool] = {}
    for name in channels:
        fn = CHANNELS.get(name)
        if fn is None:
            continue
        try:
            out[name] = bool(fn(user, subject, body))
        except Exception as exc:  # pragma: no cover - network
            log.error("digest channel %s failed for %s: %s", name, user.email, exc)
            out[name] = False
    return out


def send_one(db: Session, user: User, *, now: Optional[datetime] = None, force: bool = False, dry_run: bool = False, use_ai: bool = True) -> Dict[str, Any]:
    if not is_due(user, now=now, force=force):
        return {"user": user.email, "status": "not-due"}
    built = build(db, user, now=now, use_ai=use_ai)
    if built is None:
        if not dry_run:
            mark_sent(db, user, clock.local_today(user, now=now))
        return {"user": user.email, "status": "skipped-nothing-due"}
    subject, body, data = built
    if dry_run:
        return {"user": user.email, "status": "dry-run", "subject": subject, "body": body, "source": data["source"]}
    results = deliver(user, subject, body, data["config"]["channels"])
    ok = any(results.values())
    if ok:
        mark_sent(db, user, clock.local_today(user, now=now))
    return {"user": user.email, "status": "sent" if ok else "failed", "channels": results, "source": data["source"]}


def candidates(db: Session) -> List[User]:
    """Users with the digest switched on (cheap pre-filter; `is_due` decides)."""
    out = []
    for user in db.scalars(select(User).where(User.is_active.is_(True))):
        if user.profile is None:
            continue
        if load(user)["enabled"]:
            out.append(user)
    return out


def send_all(db: Session, *, now: Optional[datetime] = None, force: bool = False, dry_run: bool = False, only_email: Optional[str] = None, use_ai: bool = True) -> List[Dict[str, Any]]:
    if only_email:
        user = db.scalar(select(User).where(User.email == only_email.strip().lower()))
        if user is None:
            return [{"user": only_email, "status": "no-such-user"}]
        people = [user]
    else:
        people = candidates(db)
    return [send_one(db, u, now=now, force=force, dry_run=dry_run, use_ai=use_ai) for u in people]
