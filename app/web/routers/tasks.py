# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Task routes (HTMX partials) + planner + ICS exports, all board-scoped."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ...ai import enrich as ai_enrich
from ...calendars import ics
from ...calendars import links as cal_links
from ...db import get_db
from ...models import Board, User
from ...services import tasks as task_service
from ...services.planner import plan_schedule
from .. import deps

router = APIRouter(prefix="/boards/{slug}", tags=["tasks"])


def _board_partial(request: Request, db: Session, board: Board, extra: Optional[dict] = None):
    tasks = task_service.list_tasks(db, board, include_done=False)
    user = getattr(request.state, "user", None)
    ctx = {"columns": task_service.as_columns(tasks), "stats": task_service.board_stats(task_service.list_tasks(db, board, include_done=True)), "cal_links": cal_links.links_for_board(db, board), "can_edit": True, "connections": cal_links.list_connections(db, user) if user else []}
    ctx.update(extra or {})
    return deps.render(request, "boards/_board.html", ctx)


def _after_change(db: Session, board: Board, task, user: User):
    """One-way calendar update after edits (errors land on the link, not the UI)."""
    tz = user.profile.timezone if user.profile else "UTC"
    try:
        cal_links.sync_task_links(db, task, board, timezone=tz)
    except Exception:
        pass


@router.post("/tasks", dependencies=[Depends(deps.csrf_protect)])
def add_task(request: Request, title: str = Form(...), description: str = Form(""), estimate_min: int = Form(30), importance: int = Form(3), due: Optional[str] = Form(None), energy: str = Form("medium"), depends_on: Optional[str] = Form(None), use_ai: Optional[str] = Form(None), use_ai_fields: Optional[str] = Form(None), board: Board = Depends(deps.current_board_editor), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    data = {"title": title, "description": description, "estimate_min": estimate_min, "importance": importance, "due": due, "energy": energy, "depends_on": (depends_on or "").split()}
    ai_error = None
    if use_ai or use_ai_fields:
        res = ai_enrich.enrich(db, user, board, title, description, task_service.list_tasks(db, board, include_done=True), fields=bool(use_ai_fields))
        if res.get("ok"):
            data["contexts"] = res["contexts"]
            if use_ai_fields:
                if res.get("estimate_min"):
                    data["estimate_min"] = ai_enrich.bounded_adjust(res["estimate_min"], task_service.clamp_int(estimate_min, 1, 1440, 30), res.get("neighbor_avg"))
                data["importance"] = res.get("importance") or importance
                data["energy"] = res.get("energy") or energy
        else:
            ai_error = res.get("error")
    try:
        task_service.create_task(db, board, data, actor_id=user.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if deps.is_htmx(request):
        return _board_partial(request, db, board, {"ai_error": ai_error})
    return deps.redirect(f"/boards/{board.slug}/")


@router.post("/tasks/{task_id}/done", dependencies=[Depends(deps.csrf_protect)])
def mark_done(request: Request, task_id: str, board: Board = Depends(deps.current_board_editor), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    t = task_service.get_task(db, board, task_id)
    if t is None:
        raise HTTPException(404, "Task not found")
    task_service.complete_task(db, board, t, actor_id=user.id)
    _after_change(db, board, t, user)
    return _board_partial(request, db, board) if deps.is_htmx(request) else deps.redirect(f"/boards/{board.slug}/")


@router.delete("/tasks/{task_id}", dependencies=[Depends(deps.csrf_protect)])
def delete_task(request: Request, task_id: str, board: Board = Depends(deps.current_board_editor), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    t = task_service.get_task(db, board, task_id)
    if t is None:
        raise HTTPException(404, "Task not found")
    for link in cal_links.links_for_task(db, t):
        cal_links.unlink(db, user, link, delete_remote=True)
    task_service.delete_task(db, board, t, actor_id=user.id)
    return _board_partial(request, db, board) if deps.is_htmx(request) else deps.redirect(f"/boards/{board.slug}/")


@router.get("/tasks/{task_id}/edit")
def edit_task_form(request: Request, task_id: str, board: Board = Depends(deps.current_board_editor), db: Session = Depends(get_db)):
    t = task_service.get_task(db, board, task_id)
    if t is None:
        raise HTTPException(404, "Task not found")
    return deps.render(request, "boards/_task_edit.html", {"t": t})


@router.post("/tasks/{task_id}/edit", dependencies=[Depends(deps.csrf_protect)])
def edit_task(request: Request, task_id: str, title: str = Form(...), description: str = Form(""), estimate_min: int = Form(30), importance: int = Form(3), due: Optional[str] = Form(None), energy: str = Form("medium"), contexts: str = Form(""), depends_on: str = Form(""), status: str = Form("open"), board: Board = Depends(deps.current_board_editor), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    t = task_service.get_task(db, board, task_id)
    if t is None:
        raise HTTPException(404, "Task not found")
    patch = {"title": title, "description": description, "estimate_min": estimate_min, "importance": importance, "due": due, "energy": energy, "status": status,
             "contexts": [c.strip() for c in contexts.split(",") if c.strip()] or t.contexts, "depends_on": depends_on.split()}
    task_service.update_task(db, board, t, patch, actor_id=user.id)
    _after_change(db, board, t, user)
    return _board_partial(request, db, board) if deps.is_htmx(request) else deps.redirect(f"/boards/{board.slug}/")


@router.post("/tasks/{task_id}/reeval", dependencies=[Depends(deps.csrf_protect)])
def reeval_task(request: Request, task_id: str, board: Board = Depends(deps.current_board_editor), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    t = task_service.get_task(db, board, task_id)
    if t is None:
        raise HTTPException(404, "Task not found")
    res = ai_enrich.enrich(db, user, board, t.title, t.description, task_service.list_tasks(db, board, include_done=True), fields=True)
    ai_error = None
    if res.get("ok"):
        patch = {"contexts": res["contexts"]}
        if res.get("estimate_min"):
            patch["estimate_min"] = ai_enrich.bounded_adjust(res["estimate_min"], t.estimate_min, res.get("neighbor_avg"))
        if res.get("importance"):
            patch["importance"] = res["importance"]
        if res.get("energy"):
            patch["energy"] = res["energy"]
        task_service.update_task(db, board, t, patch, actor_id=user.id, actor_kind="ai")
    else:
        ai_error = res.get("error")
    return _board_partial(request, db, board, {"ai_error": ai_error})


@router.post("/tasks/reorder", dependencies=[Depends(deps.csrf_protect)])
def reorder(request: Request, payload: dict = Body(...), board: Board = Depends(deps.current_board_editor), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    task_service.reorder(db, board, payload.get("context"), [str(x) for x in (payload.get("order") or [])], actor_id=user.id)
    return _board_partial(request, db, board)


@router.post("/tasks/{task_id}/actual", dependencies=[Depends(deps.csrf_protect)])
def actual_minutes(request: Request, task_id: str, payload: dict = Body(...), board: Board = Depends(deps.current_board_editor), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    t = task_service.get_task(db, board, task_id)
    if t is not None:
        val = payload.get("actual_min")
        if isinstance(val, int) and 1 <= val <= 1440:
            task_service.update_task(db, board, t, {"last_actual_min": val}, actor_id=user.id)
    return _board_partial(request, db, board)


@router.post("/tasks/clear", dependencies=[Depends(deps.csrf_protect)])
def clear_all(request: Request, board: Board = Depends(deps.current_board_admin), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    task_service.clear_board(db, board, actor_id=user.id)
    return _board_partial(request, db, board, {"clear_plan": True})


# --- planner --------------------------------------------------------------

@router.post("/plan", dependencies=[Depends(deps.csrf_protect)])
def plan(request: Request, window: Optional[str] = Form(None), start_context: Optional[str] = Form(None), board: Board = Depends(deps.current_board), db: Session = Depends(get_db)):
    window_used = task_service.clamp_int(window, 15, 24 * 60, 480)
    tasks = task_service.tasks_by_id(db, board)
    plan_list, _grouped, total, switches = plan_schedule(tasks, window_minutes=window_used, start_context=start_context or None)
    payload = [{"id": t.id, "title": t.title, "estimate_min": t.estimate_min, "importance": t.importance, "due": t.due, "context": t.primary_context} for t in plan_list]
    return deps.render(request, "boards/_plan.html", {"plan": payload, "total": total, "switches": switches, "window_used": window_used, "start_context": start_context or ""})


@router.get("/plan.ics")
def plan_ics(ids: Optional[str] = Query(None), window: Optional[str] = Query(None), start_context: Optional[str] = Query(None), board: Board = Depends(deps.current_board), db: Session = Depends(get_db)):
    window_used = task_service.clamp_int(window, 15, 24 * 60, 480)
    tasks = task_service.tasks_by_id(db, board)
    if ids:
        planned = [tasks[i] for i in ids.split(",") if i in tasks and not tasks[i].done]
    else:
        planned, _, _, _ = plan_schedule(tasks, window_minutes=window_used, start_context=start_context or None)
    budget, chosen = 0, []
    for t in planned:
        if budget + t.estimate_min > window_used:
            break
        chosen.append(t)
        budget += t.estimate_min
    return Response(ics.plan_ics(chosen, board), media_type="text/calendar; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="flowboard-plan-{board.slug}.ics"'})


@router.get("/export.ics")
def board_export_ics(include_done: int = 0, start: Optional[str] = None, end: Optional[str] = None, board: Board = Depends(deps.current_board), db: Session = Depends(get_db)):
    tasks = task_service.list_tasks(db, board, include_done=True)
    body = ics.board_ics(tasks, board, include_done=bool(include_done), start=task_service.parse_due(start), end=task_service.parse_due(end))
    return Response(body, media_type="text/calendar; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="flowboard-{board.slug}.ics"'})


@router.get("/tasks/{task_id}.ics")
def task_export_ics(task_id: str, board: Board = Depends(deps.current_board), db: Session = Depends(get_db)):
    t = task_service.get_task(db, board, task_id)
    if t is None:
        raise HTTPException(404, "Task not found")
    return Response(ics.task_ics(t, board), media_type="text/calendar; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="flowboard-task-{t.id[:8]}.ics"'})
