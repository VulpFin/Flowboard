# Copyright (C) 2025 TG11
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
from fastapi import FastAPI, Request, Form, Body, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from typing import Optional, Dict, List
from uuid import uuid4
from datetime import datetime, timedelta

from .models import Task
from . import storage
from .planner import plan_schedule
from .ai_tagging import llm_contexts, llm_enrich, llm_enrich_contextual


load_dotenv()
app = FastAPI(title="Vulpfin Flowboard")
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)


# --- Helpers ---


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def render_board(request: Request):
    tasks = storage.list_tasks(include_done=False)
    columns = as_columns(tasks)
    return templates.TemplateResponse(
        "board_partial.html", {"request": request, "columns": columns}
    )


def auto_contexts_simple(text: str) -> List[str]:
    KEYWORDS = {
        "excel": "Excel",
        "spreadsheet": "Excel",
        "csv": "Excel",
        "sheet": "Excel",
        "email": "Email",
        "inbox": "Email",
        "outlook": "Email",
        "gmail": "Email",
        "slides": "Slides",
        "powerpoint": "Slides",
        "deck": "Slides",
        "word": "Docs",
        "document": "Docs",
        "write": "Docs",
        "docx": "Docs",
        "python": "Coding",
        "django": "Coding",
        "flask": "Coding",
        "sql": "Coding",
        "git": "Coding",
        "github": "Coding",
        "errand": "Errand",
        "pickup": "Errand",
        "post office": "Errand",
        "drop": "Errand",
        "call": "Calls",
        "phone": "Calls",
        "zoom": "Calls",
        "meeting": "Meetings",
        "design": "Design",
        "figma": "Design",
        "mockup": "Design",
        "printer": "Office",
        "lab": "Lab",
        "warehouse": "Warehouse",
    }
    t = text.lower()
    found = set()
    for k, v in KEYWORDS.items():
        if k in t:
            found.add(v)
    return sorted(found) or ["General"]


def _clamp_int(val, lo, hi, default):
    try:
        x = int(val)
        return max(lo, min(hi, x))
    except Exception:
        return default


def as_columns(tasks: List[Task]) -> Dict[str, List[Task]]:
    cols = {}
    for t in tasks:
        if t.done:
            continue  # keep the board clean; done stay in DB for context-learning
        ctx = t.contexts[0] if t.contexts else "General"
        cols.setdefault(ctx, []).append(t)
    from datetime import datetime, timedelta

    now = datetime.now()
    for ctx, lst in cols.items():

        def parse_due(d):
            if not d:
                return None
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
                try:
                    return datetime.strptime(d, fmt)
                except:
                    pass
            return None

        lst.sort(
            key=lambda t: (
                t.manual_order,
                (parse_due(t.due) or (now + timedelta(days=9999))),
                -t.importance,
                t.estimate_min,
            )
        )
    return dict(sorted(cols.items(), key=lambda kv: kv[0].lower()))


# --- Routes ---


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    tasks = storage.list_tasks(include_done=False)
    columns = as_columns(tasks)
    return templates.TemplateResponse(
        "index.html", {"request": request, "columns": columns}
    )


@app.post("/tasks", response_class=HTMLResponse)
def add_task(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    estimate_min: int = Form(30),
    importance: int = Form(3),
    due: Optional[str] = Form(None),
    energy: str = Form("medium"),
    depends_on: Optional[str] = Form(None),
    use_ai: Optional[str] = Form(None),
    use_ai_fields: Optional[str] = Form(None),
):
    # Defaults from form (clamped)
    est = _clamp_int(estimate_min, 1, 1440, 30)
    impr = _clamp_int(importance, 1, 5, 3)
    eng = (energy or "medium").strip().lower()
    if eng not in {"low", "medium", "high"}:
        eng = "medium"
    
    contexts = []

    if use_ai_fields:
        try:
            enriched = llm_enrich_contextual(title, description, storage.get_all())
            if enriched.get("ok"):
                # estimate: bounded around the user's input (est) and neighbor_avg
                raw_est = enriched.get("estimate_min")
                neighbor_avg = enriched.get("neighbor_avg")
                if raw_est:
                    from .ai_tagging import _bounded_adjust
                    est = _bounded_adjust(raw_est, est, neighbor_avg)
                # importance/energy: take if provided
                impr = enriched.get("importance", impr) or impr
                eng  = enriched.get("energy", eng) or eng
                # contexts: take if provided
                if enriched.get("contexts"):
                    contexts = enriched["contexts"]
        except Exception:
            # fall back to simple contexts if enrich failed
            contexts = []

    if not contexts:
        if use_ai:
            try:
                from .ai_tagging import llm_contexts

                contexts = llm_contexts(title, description)
            except Exception:
                contexts = auto_contexts_simple(f"{title} {description}")
        else:
            contexts = auto_contexts_simple(f"{title} {description}")

    dep_ids = (depends_on or "").split()
    t = Task(
        id=storage.new_id(),
        title=title.strip(),
        description=description.strip(),
        estimate_min=est,
        importance=impr,
        due=(due.strip() if due else None),
        contexts=contexts[:3],
        energy=eng,
        depends_on=[d for d in dep_ids if d],
    )
    storage.put(t)
    if is_htmx(request):
        return render_board(request)
    # non-HTMX: show full page
    tasks = storage.list_tasks(include_done=False)
    columns = as_columns(tasks)
    return templates.TemplateResponse(
        "index.html", {"request": request, "columns": columns}
    )


@app.post("/tasks/{task_id}/done", response_class=HTMLResponse)
def mark_done(request: Request, task_id: str):
    tasks = storage.get_all()
    t = tasks.get(task_id)
    if t:
        t.done = True
        t.completions = (t.completions or 0) + 1
        storage.put(t)
    return (
        render_board(request)
        if is_htmx(request)
        else templates.TemplateResponse(
            "index.html",
            {"request": request, "columns": as_columns(storage.list_tasks(False))},
        )
    )


@app.delete("/tasks/{task_id}", response_class=HTMLResponse)
def delete_task(request: Request, task_id: str):
    storage.delete(task_id)
    return (
        render_board(request)
        if is_htmx(request)
        else templates.TemplateResponse(
            "index.html",
            {"request": request, "columns": as_columns(storage.list_tasks(False))},
        )
    )


@app.post("/tasks/{task_id}/reeval", response_class=HTMLResponse)
def reeval_task(request: Request, task_id: str):
    tasks = storage.get_all()
    t = tasks.get(task_id)
    if not t:
        return render_board(request)
    try:
        enriched = llm_enrich_contextual(t.title, t.description, storage.get_all())
        if enriched.get("ok"):
            # estimate: bounded around current and neighbor_avg
            raw_est = enriched.get("estimate_min")
            neighbor_avg = enriched.get("neighbor_avg")
            if raw_est:
                from .ai_tagging import _bounded_adjust
                t.estimate_min = _bounded_adjust(raw_est, t.estimate_min, neighbor_avg)
            # importance/energy
            if enriched.get("importance"):
                t.importance = enriched["importance"]
            if enriched.get("energy"):
                t.energy = enriched["energy"]
            # contexts
            if enriched.get("contexts"):
                t.contexts = enriched["contexts"]
            storage.put(t)
        else:
            print(f"[flowboard.ai] reeval: skipped (no change) for {t.id}")
    except Exception as e:
        print("[flowboard.ai] reeval error:", repr(e))

    return render_board(request)


@app.post("/plan", response_class=HTMLResponse)
def plan(request: Request, window: Optional[int] = Form(None), start_context: Optional[str] = Form(None)):
    # Default to a standard workday if no window provided
    DEFAULT_WINDOW = 480  # 8 hours
    try:
        # treat 0, "", None as “no budget”
        window = int(window) if window not in (None, "",) else DEFAULT_WINDOW
    except Exception:
        window = DEFAULT_WINDOW
    # clamp to a sane range (15 min .. 24 h)
    window = max(15, min(window, 24 * 60))

    tasks = storage.get_all()
    plan_list, grouped, total, switches = plan_schedule(
        tasks, window_minutes=window, start_context=start_context or None
    )
    payload = [{
        "id": t.id, "title": t.title, "estimate_min": t.estimate_min, "importance": t.importance,
        "due": t.due, "context": (t.contexts[0] if t.contexts else "General")
    } for t in plan_list]
    return templates.TemplateResponse(
        "plan_partial.html",
        {"request": request, "plan": payload, "total": total, "switches": switches}
    )


@app.get("/ai/logs", response_class=HTMLResponse)
def ai_logs(request: Request):
    from .ai_log import read_recent

    entries = read_recent(limit=400)
    return templates.TemplateResponse(
        "ai_logs.html", {"request": request, "entries": entries}
    )


@app.post("/tasks/reorder", response_class=HTMLResponse)
def reorder(request: Request, payload: dict = Body(...)):
    ctx = payload.get("context")
    order = payload.get("order") or []
    tasks = storage.get_all()

    # apply manual order and move primary context
    for i, tid in enumerate(order):
        t = tasks.get(tid)
        if not t:
            continue
        t.manual_order = i
        # if moved: set primary context
        if ctx and (t.contexts[:1] != [ctx]):
            t.contexts = [ctx] + [c for c in t.contexts if c != ctx]
        storage.put(t)
    return render_board(request)


@app.post("/tasks/{task_id}/actual", response_class=HTMLResponse)
def actual_minutes(request: Request, task_id: str, payload: dict = Body(...)):
    tasks = storage.get_all()
    t = tasks.get(task_id)
    if t:
        val = payload.get("actual_min")
        if isinstance(val, int) and 1 <= val <= 1440:
            t.last_actual_min = val
        storage.put(t)
    return render_board(request)


@app.get("/plan/ics")
def plan_ics(
    ids: str | None = Query(
        None, description="Comma-separated task IDs in desired order"
    ),
    window: str | None = Query(None),
    start_context: str | None = Query(None),
):
    DEFAULT_WINDOW = 480
    try:
        window_used = (
            DEFAULT_WINDOW if (window is None or window == "") else int(window)
        )
    except Exception:
        window_used = DEFAULT_WINDOW
    window_used = max(15, min(window_used, 24 * 60))

    tasks = storage.get_all()

    planned = []
    if ids:
        id_list = [x for x in (ids or "").split(",") if x]
        for tid in id_list:
            t = tasks.get(tid)
            if t and not t.done:
                planned.append(t)
    else:
        planned, _, _, _ = plan_schedule(
            tasks, window_minutes=window_used, start_context=start_context or None
        )

    now = datetime.now()
    cur = now
    elapsed = 0
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Vulpfin Flowboard//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for t in planned:
        if elapsed + t.estimate_min > window_used:
            break
        dtstart = cur.strftime("%Y%m%dT%H%M%S")
        dtend = (cur + timedelta(minutes=t.estimate_min)).strftime("%Y%m%dT%H%M%S")
        summary = f"[{(t.contexts[0] if t.contexts else 'General')}] {t.title}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{t.id}-{uuid4()}@flowboard",
            f"DTSTAMP:{now.strftime('%Y%m%dT%H%M%S')}",
            f"DTSTART:{dtstart}",
            f"DTEND:{dtend}",
            f"SUMMARY:{summary}",
            "END:VEVENT",
        ]
        cur += timedelta(minutes=t.estimate_min)
        elapsed += t.estimate_min

    lines.append("END:VCALENDAR")
    ics = "\r\n".join(lines) + "\r\n"
    headers = {"Content-Disposition": 'attachment; filename="flowboard_plan.ics"'}
    return Response(content=ics, media_type="text/calendar", headers=headers)


@app.post("/tasks/clear", response_class=HTMLResponse)
def clear_all_tasks(request: Request):
    storage.clear_all()
    # Re-render an empty board and also clear the plan via an out-of-band swap
    return templates.TemplateResponse(
        "board_partial.html",
        {
            "request": request,
            "columns": {},  # no columns -> empty board
            "clear_plan": True,  # tell the template to wipe the plan panel too
        },
    )
