#!/usr/bin/env python3
# flowpilot.py — AI-ish task planner with context-aware scheduling
# Stdlib only. Run:  python flowpilot.py --help

from __future__ import annotations
import argparse, json, os, sys, uuid, datetime as dt
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Optional, Set, Tuple

DB_PATH = os.path.join(os.path.dirname(__file__), "flowpilot_tasks.json")

# ---------- Data Model ----------

@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    estimate_min: int = 30                    # estimated effort in minutes
    importance: int = 3                       # 1..5 (5 = critical)
    due: Optional[str] = None                 # ISO datetime string (e.g. 2025-10-12 17:00)
    contexts: List[str] = field(default_factory=list)  # e.g. ["Excel","Email","Errand"]
    energy: str = "medium"                    # low/medium/high
    depends_on: List[str] = field(default_factory=list) # list of task IDs
    created: str = field(default_factory=lambda: dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    done: bool = False

# ---------- Persistence ----------

def load_tasks() -> Dict[str, Task]:
    if not os.path.exists(DB_PATH):
        return {}
    with open(DB_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    tasks = {tid: Task(**t) for tid, t in raw.items()}
    return tasks

def save_tasks(tasks: Dict[str, Task]) -> None:
    serial = {tid: asdict(t) for tid, t in tasks.items()}
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(serial, f, indent=2, ensure_ascii=False)

# ---------- Lightweight "AI" helpers (heuristics) ----------

KEYWORD_CONTEXT_MAP = {
    # tool/apps
    "excel": "Excel", "spreadsheet": "Excel", "csv": "Excel",
    "sheet": "Excel", "workbook": "Excel",
    "email": "Email", "inbox": "Email", "gmail": "Email", "outlook": "Email",
    "slides": "Slides", "powerpoint": "Slides", "deck": "Slides",
    "word": "Docs", "docx": "Docs", "document": "Docs", "write": "Docs",
    "python": "Coding", "django": "Coding", "flask": "Coding", "sql": "Coding",
    "git": "Coding", "github": "Coding",
    "errand": "Errand", "pickup": "Errand", "drop": "Errand", "post office": "Errand",
    "call": "Calls", "phone": "Calls", "zoom": "Calls", "meeting": "Meetings",
    "design": "Design", "figma": "Design", "mockup": "Design",
    # devices/places
    "printer": "Office", "lab": "Lab", "warehouse": "Warehouse",
}

def auto_contexts(text: str) -> List[str]:
    t = text.lower()
    found = set()
    # greedy match on tokens/phrases
    for key, ctx in KEYWORD_CONTEXT_MAP.items():
        if key in t:
            found.add(ctx)
    # fallback if empty
    if not found:
        found.add("General")
    return sorted(found)

def parse_due(due: Optional[str]) -> Optional[dt.datetime]:
    if not due:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(due, fmt)
        except ValueError:
            continue
    raise ValueError("Unrecognized due date format. Try 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'.")

def urgency_score(now: dt.datetime, due: Optional[dt.datetime]) -> float:
    if not due: 
        return 0.0
    delta = (due - now).total_seconds() / 3600.0  # hours until due
    if delta <= 0:
        return 3.0   # overdue = very urgent
    # sooner => higher urgency, capped
    return min(2.5, 24.0/max(delta, 1e-3))

def priority_score(t: Task, now: dt.datetime) -> float:
    # Higher = more important
    due_dt = parse_due(t.due) if t.due else None
    u = urgency_score(now, due_dt)
    imp = (t.importance - 1) / 4.0  # 0..1
    # duration pressure: short tasks get a tiny boost (clear clutter)
    dur = max(t.estimate_min, 5)
    shortness = min(0.4, 20.0/dur)
    # energy nudge: assume mornings favor high-energy work
    hour = now.hour
    energy_nudge = 0.0
    if t.energy == "high" and 9 <= hour <= 13:
        energy_nudge = 0.2
    elif t.energy == "low" and 14 <= hour <= 17:
        energy_nudge = 0.1
    return 1.0*u + 1.2*imp + 0.5*shortness + energy_nudge

# ---------- Planning heuristic ----------

def topo_sort_available(tasks: Dict[str, Task]) -> List[Task]:
    # Exclude done tasks
    pending = {tid: t for tid, t in tasks.items() if not t.done}
    # Kahn’s algorithm but ignore missing deps gracefully
    indeg = {tid: 0 for tid in pending}
    graph = {tid: [] for tid in pending}
    for tid, t in pending.items():
        for dep in t.depends_on:
            if dep in pending:
                indeg[tid] += 1
                graph[dep].append(tid)
    ready = [tid for tid, d in indeg.items() if d == 0]
    order_ids = []
    while ready:
        cur = ready.pop()
        order_ids.append(cur)
        for nb in graph[cur]:
            indeg[nb] -= 1
            if indeg[nb] == 0:
                ready.append(nb)
    # Append any cyclic residues (we’ll still show them, but mark as blocked)
    residual = [tid for tid in pending if tid not in order_ids]
    order_ids.extend(residual)
    return [pending[tid] for tid in order_ids]

def plan_schedule(tasks: Dict[str, Task],
                  window_minutes: Optional[int] = None,
                  start_context: Optional[str] = None) -> Tuple[List[Task], Dict[str, List[Task]]]:
    """
    Returns an ordered task list and a grouping by context.
    Heuristic:
      1) Topo sort (deps first).
      2) Score tasks by priority_score.
      3) Greedy clustering to minimize context switches (stick with current context if competitive).
    """
    now = dt.datetime.now()
    ordered = topo_sort_available(tasks)

    # Score all, filter blocked (deps not done) at runtime
    done_set = {tid for tid, t in tasks.items() if t.done}
    def is_unblocked(t: Task) -> bool:
        return all(dep not in tasks or dep in done_set or tasks[dep].done for dep in t.depends_on)

    candidates = [t for t in ordered if is_unblocked(t)]
    # Score
    scored = [(priority_score(t, now), t) for t in candidates]
    # Start with highest score
    scored.sort(key=lambda x: x[0], reverse=True)

    # Greedy pass: keep context if near-tie
    plan: List[Task] = []
    used: Set[str] = set()
    current_ctx = start_context

    def primary_ctx(t: Task) -> str:
        return (t.contexts[0] if t.contexts else "General")

    # Build a dict by context for grouping
    by_ctx: Dict[str, List[Task]] = {}

    # Budget if window specified
    remaining = window_minutes if window_minutes else float('inf')

    while scored and remaining > 0:
        # pick best, but bump one that matches current_ctx if close
        pick_idx = 0
        best_score, best_task = scored[0]

        if current_ctx:
            # find first task sharing current_ctx within 10% score of best
            for i, (s, tk) in enumerate(scored[:8]):  # small beam
                if primary_ctx(tk) == current_ctx and s >= 0.9*best_score:
                    pick_idx = i
                    best_score, best_task = s, tk
                    break

        # context switch penalty estimate: tiny budget-aware check
        if best_task.estimate_min > remaining:
            # remove too-large tasks and continue
            scored.pop(pick_idx)
            continue

        # take it
        plan.append(best_task)
        used.add(best_task.id)
        remaining -= best_task.estimate_min

        # update grouping and context
        ctx = primary_ctx(best_task)
        by_ctx.setdefault(ctx, []).append(best_task)
        current_ctx = ctx

        # remove it from pool
        scored.pop(pick_idx)

        # Optional: prefer to continue same context next pick; nothing else to do here

    # After selecting, refine intra-context order by (earliest due, shortest first)
    for ctx, lst in by_ctx.items():
        lst.sort(key=lambda t: ((parse_due(t.due) or (now + dt.timedelta(days=9999))), t.estimate_min))

    # Finally, stitch contexts back into order that matches the greedy sequence of contexts chosen
    final: List[Task] = []
    consumed_ids: Set[str] = set()
    for chosen in plan:
        ctx = primary_ctx(chosen)
        # append the next in that context
        if by_ctx[ctx]:
            nxt = by_ctx[ctx].pop(0)
            if nxt.id not in consumed_ids:
                final.append(nxt)
                consumed_ids.add(nxt.id)
    # append any leftovers (rare)
    for ctx, lst in by_ctx.items():
        for t in lst:
            if t.id not in consumed_ids:
                final.append(t); consumed_ids.add(t.id)

    return final, group_by_context(final)

def group_by_context(tasks: List[Task]) -> Dict[str, List[Task]]:
    d: Dict[str, List[Task]] = {}
    for t in tasks:
        ctx = t.contexts[0] if t.contexts else "General"
        d.setdefault(ctx, []).append(t)
    return d

# ---------- CLI Commands ----------

def cmd_add(args):
    tasks = load_tasks()
    tid = str(uuid.uuid4())[:8]
    contexts = args.contexts or auto_contexts(args.title + " " + args.description)
    t = Task(
        id=tid,
        title=args.title,
        description=args.description or "",
        estimate_min=args.estimate,
        importance=args.importance,
        due=args.due,
        contexts=contexts,
        energy=args.energy.lower(),
        depends_on=args.depends or []
    )
    tasks[tid] = t
    save_tasks(tasks)
    print(f"Added [{tid}] {t.title}  (contexts: {', '.join(t.contexts)})")

def cmd_list(args):
    tasks = load_tasks()
    rows = [t for t in tasks.values() if (args.all or not t.done)]
    if args.context:
        rows = [t for t in rows if args.context in t.contexts]
    if not rows:
        print("No tasks.")
        return
    now = dt.datetime.now()
    rows.sort(key=lambda t: (-priority_score(t, now), parse_due(t.due) or (now + dt.timedelta(days=9999))))
    for t in rows:
        due = t.due or "-"
        mark = "✓" if t.done else "•"
        deps = f" deps:{','.join(t.depends_on)}" if t.depends_on else ""
        print(f"{mark} [{t.id}] {t.title}  ({t.estimate_min}m, imp:{t.importance}, due:{due}, ctx:{'/'.join(t.contexts)}, energy:{t.energy}{deps})")

def cmd_done(args):
    tasks = load_tasks()
    t = tasks.get(args.id)
    if not t:
        print("No such task.")
        return
    t.done = True
    save_tasks(tasks)
    print(f"Completed [{t.id}] {t.title}")

def cmd_delete(args):
    tasks = load_tasks()
    if args.id not in tasks:
        print("No such task.")
        return
    title = tasks[args.id].title
    del tasks[args.id]
    save_tasks(tasks)
    print(f"Deleted [{args.id}] {title}")

def cmd_edit(args):
    tasks = load_tasks()
    t = tasks.get(args.id)
    if not t:
        print("No such task.")
        return
    if args.title is not None: t.title = args.title
    if args.description is not None: t.description = args.description
    if args.estimate is not None: t.estimate_min = args.estimate
    if args.importance is not None: t.importance = args.importance
    if args.due is not None: t.due = args.due
    if args.energy is not None: t.energy = args.energy.lower()
    if args.contexts is not None:
        t.contexts = args.contexts if args.contexts else auto_contexts(t.title + " " + t.description)
    if args.depends is not None:
        t.depends_on = args.depends
    save_tasks(tasks)
    print(f"Updated [{t.id}] {t.title}")

def cmd_plan(args):
    tasks = load_tasks()
    final, grouped = plan_schedule(tasks, window_minutes=args.window, start_context=args.start_context)
    if not final:
        print("Nothing to plan. Add tasks or widen the time window.")
        return

    print("\n— Best Path Forward —")
    total = 0
    ctx_switches = 0
    last_ctx = None
    for i, t in enumerate(final, 1):
        ctx = t.contexts[0] if t.contexts else "General"
        if last_ctx is not None and ctx != last_ctx:
            ctx_switches += 1
        last_ctx = ctx
        due = t.due or "-"
        print(f"{i:>2}. [{t.id}] {t.title}  [{ctx}]  {t.estimate_min}m  imp:{t.importance}  due:{due}")
        total += t.estimate_min
    print(f"\nEstimated total: {total} min   |   Context switches: {ctx_switches}")

    if args.by_context:
        print("\n— Grouped by Context —")
        for ctx, lst in grouped.items():
            block = sum(x.estimate_min for x in lst)
            print(f"[{ctx}]  ({block} min)")
            for t in lst:
                print(f"   - [{t.id}] {t.title}  {t.estimate_min}m  imp:{t.importance}")

# ---------- Parser ----------

def make_parser():
    ap = argparse.ArgumentParser(prog="flowpilot", description="Context-aware task planner")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="Add a task")
    p_add.add_argument("title")
    p_add.add_argument("-d","--description", default="")
    p_add.add_argument("-e","--estimate", type=int, default=30, help="minutes, default 30")
    p_add.add_argument("-i","--importance", type=int, default=3, choices=range(1,6))
    p_add.add_argument("--due", help="YYYY-MM-DD or YYYY-MM-DD HH:MM")
    p_add.add_argument("-c","--contexts", nargs="*", help="override auto contexts (space-separated)")
    p_add.add_argument("--energy", choices=["low","medium","high"], default="medium")
    p_add.add_argument("--depends", nargs="*", help="IDs this task depends on")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("-a","--all", action="store_true", help="include completed")
    p_list.add_argument("--context", help="filter by context name")
    p_list.set_defaults(func=cmd_list)

    p_done = sub.add_parser("done", help="Mark task done")
    p_done.add_argument("id")
    p_done.set_defaults(func=cmd_done)

    p_del = sub.add_parser("delete", help="Delete a task")
    p_del.add_argument("id")
    p_del.set_defaults(func=cmd_delete)

    p_edit = sub.add_parser("edit", help="Edit a task")
    p_edit.add_argument("id")
    p_edit.add_argument("--title")
    p_edit.add_argument("--description")
    p_edit.add_argument("--estimate", type=int)
    p_edit.add_argument("--importance", type=int, choices=range(1,6))
    p_edit.add_argument("--due")
    p_edit.add_argument("--contexts", nargs="*")
    p_edit.add_argument("--energy", choices=["low","medium","high"])
    p_edit.add_argument("--depends", nargs="*")
    p_edit.set_defaults(func=cmd_edit)

    p_plan = sub.add_parser("plan", help="Generate best path forward")
    p_plan.add_argument("-w","--window", type=int, help="time window in minutes (optional)")
    p_plan.add_argument("--start-context", help="bias initial context (e.g. Excel)")
    p_plan.add_argument("--by-context", action="store_true", help="show grouped blocks")
    p_plan.set_defaults(func=cmd_plan)

    return ap

def main():
    ap = make_parser()
    args = ap.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
