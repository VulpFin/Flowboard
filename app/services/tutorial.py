# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Seeds a "Getting started" board that teaches the app by being used."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from ..models import Board, Task, User
from . import boards as board_service
from . import tasks as task_service

TUTORIAL_TASKS = [
    ("Welcome to Flowboard 👋", "This board is a hands-on tour. Each task teaches one feature. Mark tasks Done as you go — when you complete one you'll be asked a few quick questions; answering them teaches the assistant how *you* work.\n\nTip: click a task title to expand it, click a column title to collapse it.", 5, 3, "low", ["Tutorial"], 0, None),
    ("Add your first real task", "Use the *Add task* form above the board. Title is required; estimate (minutes), importance (1–5), energy (low/medium/high) and a due date help the planner. Contexts (Excel, Email, Coding…) are the columns — they group work that needs the same setup.", 5, 3, "low", ["Tutorial"], 0, None),
    ("Drag a task between columns", "Columns are contexts. Drag any card to another column to change its primary context, or reorder within a column. Order is remembered.", 3, 2, "low", ["Tutorial"], 0, None),
    ("Press *Plan* in the top bar", "The planner builds a *best path* through your open tasks for a time window (default: your work day), preferring urgent, important, short and dependency-free work while minimising context switches. Export it with *Plan .ics*.", 5, 3, "medium", ["Tutorial"], 0, None),
    ("Set your work schedule", "Open Settings → Schedule: per weekday set start/end, the maximum minutes you're willing to work and the maximum high-energy minutes, plus days off. Then use *Schedule* on a board: tasks are placed on days without exceeding your limits, and anything unfinished rolls over to the next free day automatically.", 8, 4, "medium", ["Tutorial"], 1, None),
    ("Connect an AI provider (your own key)", "Settings → AI Providers: add an API key for OpenAI, Anthropic, Gemini, Groq, OpenRouter, a local Ollama… Flowboard never uses an operator key; usage and estimated cost show under AI Usage. If you stored keys in your TG11 account, use *Sync from TG11*.", 10, 4, "medium", ["Tutorial"], 1, None),
    ("Ask the assistant to split this task", "With a provider configured, type in the Assistant panel: \"Break 'Ask the assistant to split this task' into 3 subtasks\". The assistant *proposes* changes; nothing happens until you tick the operations and press Apply. Try the quick chips too (Summarise, Prioritise, Plan this week).", 10, 4, "medium", ["Tutorial"], 2, None),
    ("Get step-by-step instructions", "Expand any task and press *How do I do this?* — the assistant writes concrete steps and stores them with the task. After completing it, rate how clear they were; that rating tunes future instructions.", 5, 3, "low", ["Tutorial"], 2, None),
    ("Add a task to your calendar", "Give a task a due date, then use *.ics* on the card (one-off file) or connect Google/Microsoft in Settings → Calendars and press *+ Calendar*. Boards also have a subscription feed URL for Apple Calendar/Outlook.", 5, 2, "low", ["Tutorial"], 3, None),
    ("Create a second board", "Boards keep areas of life apart: Work, TG11, Home, School… Boards → New board. Each board can use a different AI model (Board settings) and has its own activity log and calendar feed.", 3, 2, "low", ["Tutorial"], 3, None),
    ("Delete or archive this tutorial board", "Done touring? Board settings → Archive (keeps it) or Delete. You can recreate it any time from Settings → Account.", 2, 1, "low", ["Tutorial"], 4, None),
]


def seed_tutorial(db: Session, user: User, *, force: bool = False) -> Board:
    profile = user.profile
    if profile is not None and profile.tutorial_seeded and not force:
        existing = [b for b in board_service.list_boards_for_user(db, user, include_archived=True) if b.slug.startswith("getting-started")]
        if existing:
            return existing[0]
    board = board_service.create_board(db, user, name="Getting started", description="A guided tour of Flowboard — complete the tasks in order.", icon="🎓", color="#5cdfb7")
    today = date.today()
    ids = {}
    for i, (title, desc, est, imp, energy, ctx, day_offset, _) in enumerate(TUTORIAL_TASKS):
        t = task_service.create_task(db, board, {"title": title, "description": desc, "estimate_min": est, "importance": imp, "energy": energy, "contexts": ctx, "manual_order": i, "due": (today + timedelta(days=day_offset + 1)).isoformat() if day_offset else None}, actor_kind="system")
        t.scheduled_date = today + timedelta(days=day_offset)
        ids[i] = t.id
    # simple sequencing: each task depends on the previous one so the planner shows the order
    for i in range(1, len(TUTORIAL_TASKS)):
        t = db.get(Task, ids[i])
        if t is not None:
            t.depends_on = [ids[i - 1]]
    if profile is not None:
        profile.tutorial_seeded = True
    db.flush()
    return board
