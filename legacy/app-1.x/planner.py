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

from __future__ import annotations
from typing import Dict, List, Tuple, Set, Optional
from datetime import datetime, timedelta
from .models import Task


def parse_due(due: Optional[str]):
    if not due:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            from datetime import datetime

            return datetime.strptime(due, fmt)
        except ValueError:
            pass
    return None


def urgency_score(now, due):
    if not due:
        return 0.0
    delta = (due - now).total_seconds() / 3600
    if delta <= 0:
        return 3.0
    return min(2.5, 24.0 / max(delta, 1e-3))


def priority_score(t: Task, now: datetime) -> float:
    due_dt = parse_due(t.due)
    u = urgency_score(now, due_dt)
    imp = (t.importance - 1) / 4.0
    dur = max(t.estimate_min, 5)
    if t.last_actual_min and t.last_actual_min < dur:
        dur = (dur + t.last_actual_min) / 2  # crude average
    shortness = min(0.4, 20.0 / dur)
    hour = now.hour
    energy_nudge = (
        0.2
        if t.energy == "high" and 9 <= hour <= 13
        else (0.1 if t.energy == "low" and 14 <= hour <= 17 else 0.0)
    )
    return 1.0 * u + 1.2 * imp + 0.5 * shortness + energy_nudge


def topo_sort_available(tasks: Dict[str, Task]) -> List[Task]:
    pending = {tid: t for tid, t in tasks.items() if not t.done}
    indeg = {tid: 0 for tid in pending}
    graph = {tid: [] for tid in pending}
    for tid, t in pending.items():
        for dep in t.depends_on:
            if dep in pending:
                indeg[tid] += 1
                graph[dep].append(tid)
    ready = [tid for tid, d in indeg.items() if d == 0]
    order = []
    while ready:
        cur = ready.pop()
        order.append(cur)
        for nb in graph[cur]:
            indeg[nb] -= 1
            if indeg[nb] == 0:
                ready.append(nb)
    residual = [tid for tid in pending if tid not in order]
    order.extend(residual)
    return [pending[tid] for tid in order]


def primary_ctx(t: Task) -> str:
    return t.contexts[0] if t.contexts else "General"


def group_by_context(tasks: List[Task]):
    d = {}
    for t in tasks:
        d.setdefault(primary_ctx(t), []).append(t)
    return d


def plan_schedule(tasks: Dict[str, Task], window_minutes: Optional[int] = None, start_context: Optional[str] = None):
    now = datetime.now()
    ordered = topo_sort_available(tasks)

    # real done from DB
    real_done: Set[str] = {tid for tid, t in tasks.items() if t.done}
    # virtual done accumulates picks in this planning run
    virt_done: Set[str] = set()

    def is_unblocked(t: Task) -> bool:
        # unblocked if every dep is either not present OR done in DB OR selected earlier in this plan
        return all((dep not in tasks) or (dep in real_done) or (tasks[dep].done) or (dep in virt_done)
                   for dep in t.depends_on)

    remaining = window_minutes if window_minutes else float("inf")
    current_ctx = start_context
    plan: List[Task] = []

    # main loop: rebuild candidate pool each iteration using updated virt_done
    while remaining > 0:
        # candidates are pending, unpicked, unblocked
        cands = [t for t in ordered if (not t.done) and (t.id not in virt_done) and is_unblocked(t)]
        if not cands:
            break

        scored = sorted(((priority_score(t, now), t) for t in cands), key=lambda x: x[0], reverse=True)

        # pick best (prefer same context if close in score)
        pick_idx = 0
        best_score, best_task = scored[0]
        if current_ctx:
            for i, (s, tk) in enumerate(scored):
                if primary_ctx(tk) == current_ctx and s >= 0.9 * best_score:
                    pick_idx = i
                    best_score, best_task = s, tk
                    break

        est = max(5, int(best_task.estimate_min))
        if est > remaining:
            # BEST-FIT fallback: largest task that fits
            best_fit_idx, best_fit_est = -1, -1
            for i, (_, tk) in enumerate(scored):
                e = max(5, int(tk.estimate_min))
                if e <= remaining and e > best_fit_est:
                    best_fit_idx, best_fit_est = i, e
            if best_fit_idx == -1:
                # allow tiny overflow (10%) to avoid getting stuck on a near-fit
                next_est = max(5, int(scored[0][1].estimate_min))
                if remaining > 0 and next_est <= int(remaining * 1.10):
                    pick_idx = 0
                    best_task = scored[0][1]
                    est = next_est
                else:
                    break
            else:
                pick_idx = best_fit_idx
                best_task = scored[pick_idx][1]
                est = best_fit_est

        plan.append(best_task)
        virt_done.add(best_task.id)          # <- virtually complete it
        current_ctx = primary_ctx(best_task)
        remaining -= est
        # loop continues; new children can now unlock
    # --- end selection loop ---

    # group/sort and finalize (unchanged)
    by_ctx = {}
    for t in plan:
        by_ctx.setdefault(primary_ctx(t), []).append(t)
    for ctx, lst in by_ctx.items():
        lst.sort(key=lambda t: ((parse_due(t.due) or (now + timedelta(days=9999))), t.estimate_min))

    final, seen = [], set()
    for chosen in plan:
        ctx = primary_ctx(chosen)
        if by_ctx[ctx]:
            nxt = by_ctx[ctx].pop(0)
            if nxt.id not in seen:
                final.append(nxt); seen.add(nxt.id)
    for ctx, lst in by_ctx.items():
        for t in lst:
            if t.id not in seen:
                final.append(t); seen.add(t.id)

    grouped = group_by_context(final)
    ctx_switches = sum(1 for i in range(1, len(final)) if primary_ctx(final[i]) != primary_ctx(final[i-1]))
    total = sum(t.estimate_min for t in final)
    print("planned count:", len(final), "total min:", total, "remaining:", remaining, "window_minutes:", window_minutes)
    return final, grouped, total, ctx_switches

