# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Task service.  Every function takes an already-authorized `Board`."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Board, Task, TaskActivity, utcnow

VALID_ENERGY = {"low", "medium", "high"}
DUE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%dT%H:%M")


def parse_due(due: Optional[str]) -> Optional[datetime]:
    if not due:
        return None
    due = due.strip()
    for fmt in DUE_FORMATS:
        try:
            return datetime.strptime(due, fmt)
        except ValueError:
            continue
    return None


def normalize_due(due: Optional[str]) -> Optional[str]:
    dt = parse_due(due)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d %H:%M") if (dt.hour or dt.minute) else dt.strftime("%Y-%m-%d")


def clamp_int(val: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(val)))
    except Exception:
        return default


KEYWORD_CONTEXTS = {
    "excel": "Excel", "spreadsheet": "Excel", "csv": "Excel", "sheet": "Excel",
    "email": "Email", "inbox": "Email", "outlook": "Email", "gmail": "Email",
    "slides": "Slides", "powerpoint": "Slides", "deck": "Slides",
    "word": "Docs", "document": "Docs", "write": "Docs", "docx": "Docs",
    "python": "Coding", "django": "Coding", "flask": "Coding", "sql": "Coding", "git": "Coding", "github": "Coding", "code": "Coding",
    "errand": "Errand", "pickup": "Errand", "post office": "Errand", "drop": "Errand",
    "call": "Calls", "phone": "Calls", "zoom": "Calls", "meeting": "Meetings",
    "design": "Design", "figma": "Design", "mockup": "Design",
    "printer": "Office", "lab": "Lab", "warehouse": "Warehouse",
}


def auto_contexts(text: str) -> List[str]:
    t = (text or "").lower()
    found = {v for k, v in KEYWORD_CONTEXTS.items() if k in t}
    return sorted(found) or ["General"]


def list_tasks(db: Session, board: Board, include_done: bool = False) -> List[Task]:
    q = select(Task).where(Task.board_id == board.id)
    if not include_done:
        q = q.where(Task.done.is_(False))
    return list(db.scalars(q.order_by(Task.manual_order, Task.created_at)))


def tasks_by_id(db: Session, board: Board) -> Dict[str, Task]:
    return {t.id: t for t in list_tasks(db, board, include_done=True)}


def get_task(db: Session, board: Board, task_id: str) -> Optional[Task]:
    """Board-scoped lookup - a task id from another board returns None."""
    t = db.get(Task, task_id)
    if t is None or t.board_id != board.id:
        # allow legacy 8-char ids as well
        t = db.scalar(select(Task).where(Task.board_id == board.id, Task.legacy_id == task_id))
    return t


def _resolve_dep_ids(db: Session, board: Board, deps: Iterable[str]) -> List[str]:
    out = []
    for d in deps:
        d = (d or "").strip()
        if not d:
            continue
        t = get_task(db, board, d)
        if t is not None:
            out.append(t.id)
    return out


def create_task(db: Session, board: Board, data: Dict[str, Any], *, actor_id: Optional[str] = None, actor_kind: str = "user") -> Task:
    title = (data.get("title") or "").strip()
    if not title:
        raise ValueError("Title is required")
    energy = (data.get("energy") or "medium").strip().lower()
    contexts = data.get("contexts") or auto_contexts(f"{title} {data.get('description', '')}")
    due = normalize_due(data.get("due"))
    task = Task(
        board_id=board.id,
        created_by_id=actor_id,
        title=title[:300],
        description=(data.get("description") or "").strip(),
        estimate_min=clamp_int(data.get("estimate_min"), 1, 1440, 30),
        importance=clamp_int(data.get("importance"), 1, 5, 3),
        energy=energy if energy in VALID_ENERGY else "medium",
        due=due,
        due_at=parse_due(due),
        manual_order=clamp_int(data.get("manual_order"), 0, 100000, 0),
        parent_id=data.get("parent_id"),
        ai_generated=actor_kind == "ai",
        status="open",
    )
    task.contexts = [str(c)[:32] for c in contexts][:3]
    task.depends_on = _resolve_dep_ids(db, board, data.get("depends_on") or [])
    task.tags = data.get("tags") or []
    db.add(task)
    db.flush()
    log_activity(db, board, task_id=task.id, actor_id=actor_id, actor_kind=actor_kind, action="created", summary=f"Created “{task.title}”")
    return task


UPDATABLE = {"title", "description", "estimate_min", "importance", "energy", "due", "contexts", "depends_on", "tags", "manual_order", "status", "parent_id", "last_actual_min", "scheduled_date", "instructions"}


def update_task(db: Session, board: Board, task: Task, patch: Dict[str, Any], *, actor_id: Optional[str] = None, actor_kind: str = "user") -> Task:
    assert task.board_id == board.id
    changes = {}
    for key, value in patch.items():
        if key not in UPDATABLE:
            continue
        if key == "title":
            value = (value or "").strip()[:300]
            if not value:
                continue
        elif key == "estimate_min":
            value = clamp_int(value, 1, 1440, task.estimate_min)
        elif key == "importance":
            value = clamp_int(value, 1, 5, task.importance)
        elif key == "manual_order":
            value = clamp_int(value, 0, 100000, task.manual_order)
        elif key == "last_actual_min":
            value = clamp_int(value, 1, 1440, 0) or None
        elif key == "energy":
            value = (value or "").strip().lower()
            if value not in VALID_ENERGY:
                continue
        elif key == "due":
            value = normalize_due(value) if value else None
            task.due_at = parse_due(value)
        elif key == "contexts":
            value = [str(c)[:32] for c in (value or [])][:3]
        elif key == "depends_on":
            value = _resolve_dep_ids(db, board, value or [])
        elif key == "status":
            if value not in {"open", "blocked", "done", "archived"}:
                continue
        elif key == "description":
            value = (value or "").strip()
        elif key == "instructions":
            value = (value or "").strip()[:20000]
        elif key == "scheduled_date":
            if isinstance(value, str):
                try:
                    value = date.fromisoformat(value.strip()[:10]) if value.strip() else None
                except ValueError:
                    continue
            elif value is not None and not isinstance(value, date):
                continue
        old = getattr(task, key)
        if old != value:
            changes[key] = {"from": old, "to": value}
            setattr(task, key, value)
    if changes:
        if "status" in changes:
            task.done = task.status == "done"
            task.completed_at = utcnow() if task.done else None
        db.flush()
        log_activity(db, board, task_id=task.id, actor_id=actor_id, actor_kind=actor_kind, action="updated", summary=f"Updated “{task.title}” ({', '.join(changes)})", data=changes)
    return task


def complete_task(db: Session, board: Board, task: Task, *, actor_id: Optional[str] = None, actor_kind: str = "user", done: bool = True) -> Task:
    assert task.board_id == board.id
    task.done = done
    task.status = "done" if done else "open"
    task.completed_at = utcnow() if done else None
    if done:
        task.completions = (task.completions or 0) + 1
    db.flush()
    log_activity(db, board, task_id=task.id, actor_id=actor_id, actor_kind=actor_kind, action="completed" if done else "reopened", summary=f"{'Completed' if done else 'Reopened'} “{task.title}”")
    return task


def delete_task(db: Session, board: Board, task: Task, *, actor_id: Optional[str] = None, actor_kind: str = "user") -> None:
    assert task.board_id == board.id
    title = task.title
    db.delete(task)
    db.flush()
    log_activity(db, board, task_id=None, actor_id=actor_id, actor_kind=actor_kind, action="deleted", summary=f"Deleted “{title}”")


def reorder(db: Session, board: Board, context: Optional[str], ordered_ids: List[str], *, actor_id: Optional[str] = None) -> None:
    for i, tid in enumerate(ordered_ids):
        t = get_task(db, board, tid)
        if t is None:
            continue
        t.manual_order = i
        if context and t.contexts[:1] != [context]:
            t.contexts = [context] + [c for c in t.contexts if c != context]
    db.flush()


def clear_board(db: Session, board: Board, *, actor_id: Optional[str] = None) -> int:
    n = 0
    for t in list_tasks(db, board, include_done=True):
        db.delete(t)
        n += 1
    db.flush()
    log_activity(db, board, actor_id=actor_id, action="cleared", summary=f"Removed all {n} tasks")
    return n


def as_columns(tasks: List[Task]) -> Dict[str, List[Task]]:
    cols: Dict[str, List[Task]] = {}
    for t in tasks:
        if t.done:
            continue
        cols.setdefault(t.primary_context, []).append(t)
    far = utcnow() + timedelta(days=9999)
    for lst in cols.values():
        lst.sort(key=lambda t: (t.manual_order, t.due_at or far, -t.importance, t.estimate_min))
    return dict(sorted(cols.items(), key=lambda kv: kv[0].lower()))


def log_activity(db: Session, board: Board, *, action: str, summary: str, task_id: Optional[str] = None, actor_id: Optional[str] = None, actor_kind: str = "user", data: Optional[Dict] = None) -> None:
    db.add(TaskActivity(board_id=board.id, task_id=task_id, actor_id=actor_id, actor_kind=actor_kind, action=action, summary=summary[:500], data_json=json.dumps(data or {}, default=str)))


def recent_activity(db: Session, board: Board, limit: int = 50) -> List[TaskActivity]:
    return list(db.scalars(select(TaskActivity).where(TaskActivity.board_id == board.id).order_by(TaskActivity.created_at.desc()).limit(limit)))


def board_stats(tasks: List[Task]) -> Dict[str, Any]:
    open_tasks = [t for t in tasks if not t.done]
    return {
        "total": len(tasks),
        "open": len(open_tasks),
        "done": len(tasks) - len(open_tasks),
        "overdue": sum(1 for t in open_tasks if t.is_overdue),
        "blocked": sum(1 for t in open_tasks if t.status == "blocked"),
        "minutes_open": sum(t.estimate_min for t in open_tasks),
    }
