# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Per-task "How do I do this?" - concrete, ordered steps written by the
assistant and stored on the task (`Task.instructions`).  The user's
calibration profile is included so the steps match how concrete they need
things to be."""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy.orm import Session

from ..models import Board, Task, User
from ..services import reflections
from ..services import tasks as task_service
from .client import AIClient
from .types import ChatMessage, ProviderError

SYSTEM = """You write practical, step-by-step instructions for completing ONE task from a personal planning board.
Rules:
- Output Markdown only: an optional one-line goal, then a numbered list of 3-12 concrete steps, then (optional) a short 'Watch out for' list.
- Each step must be an action the person can do right now; include commands, menu paths, names of files/tools when implied by the task.
- Match the task's estimate: do not propose more work than fits the estimate; if the estimate is clearly too small, say so in one line at the end.
- Be specific, no motivational filler."""


def generate(db: Session, user: User, board: Board, task: Task, *, model_ref: str = None) -> Dict[str, Any]:
    client = AIClient(db, user, board, model_ref)
    if client.resolved is None:
        return {"ok": False, "error": "no AI provider configured"}
    calibration = reflections.prompt_summary(user)
    siblings = [t for t in task_service.list_tasks(db, board, include_done=True) if t.id != task.id][:20]
    ctx = "\n".join(f"- {'[done] ' if t.done else ''}{t.title} ({t.primary_context}, {t.estimate_min}m)" for t in siblings)
    user_msg = (
        f"Task: {task.title}\nDetails: {task.description or '(none)'}\nContext: {task.primary_context}\nEstimate: {task.estimate_min} min · importance {task.importance}/5 · energy {task.energy}"
        + (f"\nDue: {task.due}" if task.due else "")
        + (f"\n\nOther tasks on this board (for context only):\n{ctx}" if ctx else "")
        + (f"\n\n{calibration}" if calibration else "")
        + "\n\nWrite the instructions."
    )
    try:
        res = client.chat([ChatMessage(role="system", content=SYSTEM), ChatMessage(role="user", content=user_msg)], max_tokens=1200, temperature=0.3, operation="instructions")
    except ProviderError as exc:
        return {"ok": False, "error": exc.friendly()}
    text = (res.content or "").strip()
    if not text:
        return {"ok": False, "error": "the model returned nothing"}
    task_service.update_task(db, board, task, {"instructions": text}, actor_id=user.id, actor_kind="ai")
    return {"ok": True, "instructions": text, "model": client.model_ref}
