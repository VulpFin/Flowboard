# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Board assistant.

The assistant never edits the board directly.  It returns a *proposal*
(`AIChangeSet`) made of validated operations; the user reviews and applies
(all or a subset).  Read-only requests (summaries, prioritisation advice,
searches) come back as a message with zero operations.

The same JSON schema is used for every provider: providers with tool calling
receive it as a tool, others through JSON mode / prompting (see
`AIClient.structured`).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import AIChangeSet, Board, Task, User, utcnow
from ..services import tasks as task_service
from .client import AIClient
from .types import ChatMessage

OPERATIONS = ("create_task", "update_task", "complete_task", "reopen_task", "delete_task")

TASK_FIELDS_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 300},
        "description": {"type": "string"},
        "estimate_min": {"type": "integer", "minimum": 1, "maximum": 1440},
        "importance": {"type": "integer", "minimum": 1, "maximum": 5},
        "energy": {"type": "string", "enum": ["low", "medium", "high"]},
        "due": {"type": ["string", "null"], "description": "YYYY-MM-DD or YYYY-MM-DD HH:MM, null to clear"},
        "contexts": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "depends_on": {"type": "array", "items": {"type": "string"}, "description": "task ids or temp ids of tasks created earlier in this proposal"},
        "status": {"type": "string", "enum": ["open", "blocked"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "scheduled_date": {"type": ["string", "null"], "description": "YYYY-MM-DD day the task should be worked on (respect the user's daily capacity), null to unschedule"},
        "scheduled_start": {"type": ["string", "null"], "description": "HH:MM start time inside that day; put high-energy work in the user's high-energy blocks"},
        "assigned_to_id": {"type": ["string", "null"], "description": "id of the board member who should do this (only ids listed in the members block), null to unassign"},
    },
}

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "description": "Short answer / explanation for the user in Markdown."},
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": list(OPERATIONS)},
                    "task_id": {"type": "string", "description": "existing task id (required for update/complete/reopen/delete)"},
                    "temp_id": {"type": "string", "description": "for create_task: a short label other operations can reference in depends_on"},
                    "fields": TASK_FIELDS_SCHEMA,
                    "reason": {"type": "string"},
                },
                "required": ["op"],
            },
        },
    },
    "required": ["message", "operations"],
}

SYSTEM_PROMPT = """You are Flowboard's planning assistant. You help the user manage ONE board of tasks.
You can answer questions (summaries, priorities, overdue/blocked work, effort estimates, schedules) and you can PROPOSE changes.
Rules:
- Never claim a change has been made. You only propose operations; the user approves them.
- Prefer few, precise operations. Do not delete tasks unless the user clearly asks.
- When breaking a task into subtasks, create_task each subtask with a temp_id and set depends_on to sequence them if there is an order; keep the original task unless asked to remove it.
- Due dates: today is {today} ({weekday}). Use YYYY-MM-DD (add HH:MM only when a time is meaningful). "Next week" means the following Monday-Friday.
- Contexts are short setup labels (Excel, Email, Coding, Docs, Slides, Calls, Errand, Design, Meetings, Lab, Office, General...). Keep existing contexts unless asked.
- Estimates are minutes (5-1440). Importance 1-5 (5 critical). Energy low/medium/high.
- `scheduled_date` is the day the user plans to *work on* a task (distinct from `due`). When scheduling, never exceed the daily capacity given in the work-schedule block; spill extra work to the next free day.
- `scheduled_start` (HH:MM) is the slot inside that day. Respect the energy curve: high-energy tasks belong in high-energy blocks, and never place work on top of the meeting minutes reported for that day.
- On a shared board you may set `assigned_to_id` to one of the member ids listed in the members block; schedule that task against *that* member's free minutes.
- If a calibration block is present, apply it: scale estimates by the user's real time ratio, and be more concrete when their clarity ratings are low.
- If the request is ambiguous, ask a clarifying question in `message` and return no operations.
- `message` should be concise Markdown. When you return operations, summarise them in `message` too.
"""


def serialise_board(tasks: List[Task], include_done: bool = False, limit: int = 200) -> str:
    rows = []
    for t in tasks:
        if t.done and not include_done:
            continue
        row = {
            "id": t.id[:8] if False else t.id,
            "title": t.title,
            "ctx": t.primary_context,
            "est": t.estimate_min,
            "imp": t.importance,
            "energy": t.energy,
        }
        if t.description:
            row["desc"] = t.description[:300]
        if t.due:
            row["due"] = t.due
        if t.status == "blocked":
            row["blocked"] = True
        if t.depends_on:
            row["deps"] = t.depends_on
        if t.done:
            row["done"] = True
        if t.scheduled_date:
            row["scheduled"] = t.scheduled_date.isoformat()
        if t.scheduled_start:
            row["at"] = t.scheduled_start
        if t.assigned_to_id:
            row["assigned_to_id"] = t.assigned_to_id
        if t.instructions:
            row["has_instructions"] = True
        if t.is_overdue:
            row["overdue"] = True
        rows.append(row)
        if len(rows) >= limit:
            break
    return json.dumps(rows, ensure_ascii=False)


def user_context_block(user: Optional[User], tasks: List[Task], *, db: Optional[Session] = None, board: Optional[Board] = None) -> str:
    """Calibration profile + work schedule (+ energy curve, meetings and, on a
    shared board, each member's free minutes) for this user.  Empty if unknown."""
    if user is None:
        return ""
    from ..services import reflections, schedule

    parts = []
    cal = reflections.prompt_summary(user)
    if cal:
        parts.append(cal)
    try:
        parts.append(schedule.schedule_summary_for_ai(user, tasks, db=db, board=board))
    except Exception:
        pass
    return "\n\n".join(parts)


def build_messages(board: Board, tasks: List[Task], request_text: str, history: Optional[List[Dict[str, str]]] = None, include_done: bool = False, user: Optional[User] = None, db: Optional[Session] = None) -> List[ChatMessage]:
    now = datetime.now()
    msgs = [ChatMessage(role="system", content=SYSTEM_PROMPT.format(today=now.strftime("%Y-%m-%d"), weekday=now.strftime("%A")))]
    stats = task_service.board_stats(tasks)
    msgs.append(ChatMessage(role="system", content=f"Board: {board.name}. {board.description or ''}\nStats: {json.dumps(stats)}\nTasks (JSON): {serialise_board(tasks, include_done)}"))
    ctx = user_context_block(user, tasks, db=db, board=board)
    if ctx:
        msgs.append(ChatMessage(role="system", content=ctx))
    for h in history or []:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append(ChatMessage(role=h["role"], content=h["content"][:4000]))
    msgs.append(ChatMessage(role="user", content=request_text.strip()[:6000]))
    return msgs


def _validate_operations(ops: List[Dict[str, Any]], known_ids: Dict[str, Task], member_ids: Optional[set] = None) -> List[Dict[str, Any]]:
    """Drop operations that reference unknown tasks / are malformed; normalise."""
    cleaned: List[Dict[str, Any]] = []
    temp_ids: set = set()
    for raw in ops or []:
        if not isinstance(raw, dict) or raw.get("op") not in OPERATIONS:
            continue
        op = {"op": raw["op"], "reason": str(raw.get("reason") or "")[:300], "fields": dict(raw.get("fields") or {})}
        if "assigned_to_id" in op["fields"]:
            val = op["fields"]["assigned_to_id"]
            if val and str(val) not in (member_ids or set()):
                op["fields"].pop("assigned_to_id")  # never assign to someone who is not a member
        if op["op"] == "create_task":
            if not (op["fields"].get("title") or "").strip():
                continue
            op["temp_id"] = str(raw.get("temp_id") or f"new{len(cleaned) + 1}")[:40]
            temp_ids.add(op["temp_id"])
            deps = []
            for d in op["fields"].get("depends_on") or []:
                d = str(d)
                if d in known_ids or d in temp_ids:
                    deps.append(d)
            op["fields"]["depends_on"] = deps
        else:
            tid = str(raw.get("task_id") or "")
            task = known_ids.get(tid)
            if task is None:
                # allow prefix match on id (models sometimes shorten)
                matches = [t for k, t in known_ids.items() if k.startswith(tid)] if len(tid) >= 6 else []
                task = matches[0] if len(matches) == 1 else None
            if task is None:
                continue
            op["task_id"] = task.id
            op["task_title"] = task.title
            if op["op"] == "update_task":
                deps = []
                for d in op["fields"].get("depends_on") or []:
                    d = str(d)
                    if d in known_ids or d in temp_ids:
                        deps.append(d)
                if "depends_on" in op["fields"]:
                    op["fields"]["depends_on"] = deps
                if not op["fields"]:
                    continue
        cleaned.append(op)
    return cleaned


def propose(db: Session, user: User, board: Board, request_text: str, *, model_ref: Optional[str] = None, history: Optional[List[Dict[str, str]]] = None) -> AIChangeSet:
    tasks = task_service.list_tasks(db, board, include_done=True)
    known = {t.id: t for t in tasks}
    client = AIClient(db, user, board, model_ref)
    data = client.structured(
        build_messages(board, tasks, request_text, history, user=user, db=db),
        schema=PROPOSAL_SCHEMA,
        name="propose_changes",
        description="Answer the user and propose zero or more board operations for review.",
        operation="assistant",
    )
    ops = _validate_operations(data.get("operations") or [], known, {m.user_id for m in board.memberships})
    cs = AIChangeSet(
        board_id=board.id,
        user_id=user.id,
        model_ref=client.model_ref or "",
        request_text=request_text[:6000],
        assistant_message=str(data.get("message") or "")[:8000],
        operations_json=json.dumps(ops, ensure_ascii=False),
        status="proposed" if ops else "applied",  # nothing to apply => already "done"
    )
    db.add(cs)
    db.flush()
    return cs


def get_change_set(db: Session, user: User, board: Board, cs_id: str) -> Optional[AIChangeSet]:
    cs = db.get(AIChangeSet, cs_id)
    if cs is None or cs.board_id != board.id or cs.user_id != user.id:
        return None
    return cs


def apply_change_set(db: Session, user: User, board: Board, cs: AIChangeSet, selected: Optional[List[int]] = None) -> Dict[str, Any]:
    """Apply the selected operation indexes (all if None)."""
    assert cs.board_id == board.id and cs.user_id == user.id
    if cs.status not in ("proposed", "partial"):
        return {"applied": 0, "skipped": 0, "errors": ["change set already processed"]}
    ops = json.loads(cs.operations_json or "[]")
    chosen = set(range(len(ops))) if selected is None else set(selected)
    temp_map: Dict[str, str] = {}
    applied, skipped, errors = 0, 0, []
    for i, op in enumerate(ops):
        if i not in chosen:
            skipped += 1
            continue
        try:
            fields = dict(op.get("fields") or {})
            if "depends_on" in fields:
                fields["depends_on"] = [temp_map.get(d, d) for d in fields["depends_on"]]
            if op["op"] == "create_task":
                t = task_service.create_task(db, board, fields, actor_id=user.id, actor_kind="ai")
                temp_map[op.get("temp_id", "")] = t.id
            else:
                t = task_service.get_task(db, board, op["task_id"])
                if t is None:
                    raise ValueError("task no longer exists")
                if op["op"] == "update_task":
                    task_service.update_task(db, board, t, fields, actor_id=user.id, actor_kind="ai")
                elif op["op"] == "complete_task":
                    task_service.complete_task(db, board, t, actor_id=user.id, actor_kind="ai", done=True)
                elif op["op"] == "reopen_task":
                    task_service.complete_task(db, board, t, actor_id=user.id, actor_kind="ai", done=False)
                elif op["op"] == "delete_task":
                    task_service.delete_task(db, board, t, actor_id=user.id, actor_kind="ai")
            applied += 1
        except Exception as exc:  # keep going; report per-op errors
            errors.append(f"op {i + 1} ({op.get('op')}): {exc}")
    cs.status = "applied" if not errors and skipped == 0 else "partial"
    cs.applied_at = utcnow()
    cs.result_json = json.dumps({"applied": applied, "skipped": skipped, "errors": errors, "temp_map": temp_map})
    db.flush()
    task_service.log_activity(db, board, actor_id=user.id, actor_kind="ai", action="ai_applied", summary=f"Applied {applied} AI-proposed change(s)")
    return {"applied": applied, "skipped": skipped, "errors": errors}


def reject_change_set(db: Session, cs: AIChangeSet) -> None:
    if cs.status in ("proposed", "partial"):
        cs.status = "rejected"
        db.flush()


QUICK_PROMPTS = [
    ("Summarise this board", "Give me a concise summary of this board: what's most urgent, what's overdue or blocked, and total estimated effort."),
    ("Prioritise", "Look at the open tasks and propose importance/due-date adjustments so the most valuable and urgent work is clearly first. Explain briefly."),
    ("Overdue & blocked", "List overdue and blocked tasks and suggest what to do about each."),
    ("Plan this week", "Build a realistic schedule for the rest of this week from the open tasks, assigning due dates (propose updates)."),
    ("Estimate effort", "Review the open tasks' estimates and propose corrections where they look unrealistic (use my calibration data if present)."),
    ("Daily briefing", "Give me a short briefing for today: what is scheduled or due today, what is overdue, how much capacity I have left according to my work schedule, and the single best task to start with."),
    ("Notes → tasks", "Here are my raw notes. Turn them into well-formed tasks (title, estimate, importance, energy, context, due date if mentioned) and propose creating them:\n\n"),
    ("Fit my schedule", "Assign scheduled_date to every open task over the coming days without exceeding my daily capacity, respecting dependencies and due dates (propose updates)."),
]
