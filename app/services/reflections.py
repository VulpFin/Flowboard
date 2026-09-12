# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Completion reflections → per-user calibration profile.

After finishing a task the user can answer a few questions (actual minutes,
actual energy, clarity, difficulty, notes).  From these we derive a compact
*calibration profile* that is (a) injected into every AI prompt for that user
("this person takes ~1.4× their estimates on Coding tasks") and (b) used to
scale new estimates.  Nothing is trained; it lives in the context window.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Board, Task, TaskReflection, User, UserProfile, utcnow

ENERGY_RANK = {"low": 0, "medium": 1, "high": 2}
MIN_SAMPLES = 3


def record(db: Session, user: User, board: Board, task: Task, *, actual_min: Optional[int], actual_energy: Optional[str], clarity: Optional[int], difficulty: Optional[int], notes: str = "") -> TaskReflection:
    r = TaskReflection(
        task_id=task.id, board_id=board.id, user_id=user.id, title=task.title[:300], context=task.primary_context[:32],
        estimate_min=task.estimate_min, actual_min=actual_min if actual_min and 1 <= actual_min <= 1440 else None,
        planned_energy=task.energy, actual_energy=actual_energy if actual_energy in ENERGY_RANK else None,
        clarity=clarity if clarity and 1 <= clarity <= 5 else None, difficulty=difficulty if difficulty and 1 <= difficulty <= 5 else None,
        had_instructions=bool(task.instructions), notes=(notes or "").strip()[:2000],
    )
    db.add(r)
    if r.actual_min:
        task.last_actual_min = r.actual_min
    db.flush()
    refresh_profile(db, user)
    return r


def list_for_user(db: Session, user: User, limit: int = 200) -> List[TaskReflection]:
    return list(db.scalars(select(TaskReflection).where(TaskReflection.user_id == user.id).order_by(TaskReflection.created_at.desc()).limit(limit)))


def compute_profile(rows: List[TaskReflection]) -> Dict[str, Any]:
    ratios: List[float] = []
    by_ctx: Dict[str, List[float]] = defaultdict(list)
    energy_delta: List[int] = []
    energy_by_ctx: Dict[str, List[int]] = defaultdict(list)
    clarity: List[int] = []
    clarity_with_instr: List[int] = []
    difficulty: List[int] = []
    notes: List[str] = []
    for r in rows:
        if r.actual_min and r.estimate_min:
            ratio = r.actual_min / r.estimate_min
            ratio = max(0.2, min(ratio, 5.0))
            ratios.append(ratio)
            by_ctx[r.context].append(ratio)
        if r.actual_energy and r.planned_energy in ENERGY_RANK:
            d = ENERGY_RANK[r.actual_energy] - ENERGY_RANK[r.planned_energy]
            energy_delta.append(d)
            energy_by_ctx[r.context].append(d)
        if r.clarity:
            clarity.append(r.clarity)
            if r.had_instructions:
                clarity_with_instr.append(r.clarity)
        if r.difficulty:
            difficulty.append(r.difficulty)
        if r.notes:
            notes.append(r.notes[:160])

    def avg(xs):
        return round(sum(xs) / len(xs), 2) if xs else None

    prof: Dict[str, Any] = {
        "samples": len(rows),
        "time_ratio": avg(ratios),
        "time_ratio_samples": len(ratios),
        "time_ratio_by_context": {c: {"ratio": avg(v), "n": len(v)} for c, v in by_ctx.items() if len(v) >= 1},
        "energy_delta": avg(energy_delta),  # +1 = tasks felt harder than planned
        "energy_delta_by_context": {c: avg(v) for c, v in energy_by_ctx.items()},
        "clarity_avg": avg(clarity),
        "clarity_with_instructions_avg": avg(clarity_with_instr),
        "difficulty_avg": avg(difficulty),
        "recent_notes": notes[:5],
        "updated_at": utcnow().isoformat(),
    }
    return prof


def refresh_profile(db: Session, user: User) -> Dict[str, Any]:
    rows = list_for_user(db, user, limit=300)
    prof = compute_profile(rows)
    profile: Optional[UserProfile] = user.profile
    if profile is not None:
        profile.calibration_json = json.dumps(prof)
        db.flush()
    return prof


def get_profile(user: User) -> Dict[str, Any]:
    try:
        return json.loads(user.profile.calibration_json or "{}") if user.profile else {}
    except Exception:
        return {}


def time_multiplier(user: User, context: Optional[str] = None) -> float:
    """Scale factor for estimates, bounded to [0.5, 2.0], only with enough data."""
    prof = get_profile(user)
    if context:
        c = (prof.get("time_ratio_by_context") or {}).get(context)
        if c and c.get("n", 0) >= MIN_SAMPLES and c.get("ratio"):
            return max(0.5, min(2.0, float(c["ratio"])))
    if prof.get("time_ratio_samples", 0) >= MIN_SAMPLES and prof.get("time_ratio"):
        return max(0.5, min(2.0, float(prof["time_ratio"])))
    return 1.0


def adjusted_estimate(user: User, estimate_min: int, context: Optional[str] = None) -> int:
    m = time_multiplier(user, context)
    return max(5, min(1440, int(round(estimate_min * m)))) if m != 1.0 else estimate_min


def prompt_summary(user: User) -> str:
    """Compact natural-language calibration block for AI system prompts."""
    prof = get_profile(user)
    if not prof or not prof.get("samples"):
        return ""
    parts = [f"Calibration from {prof['samples']} completed-task reflections by this user:"]
    if prof.get("time_ratio") and prof.get("time_ratio_samples", 0) >= 2:
        r = prof["time_ratio"]
        parts.append(f"- Actual time is on average {r:.2f}× the estimate ({'under' if r < 1 else 'over'}-estimates); scale suggested estimates accordingly.")
    ctxs = [(c, v) for c, v in (prof.get("time_ratio_by_context") or {}).items() if v.get("n", 0) >= 2]
    if ctxs:
        parts.append("- By context: " + ", ".join(f"{c} {v['ratio']:.2f}× (n={v['n']})" for c, v in sorted(ctxs)[:8]))
    if prof.get("energy_delta") is not None and abs(prof["energy_delta"]) >= 0.3:
        parts.append(f"- Tasks tend to feel {'more' if prof['energy_delta'] > 0 else 'less'} draining than planned (energy delta {prof['energy_delta']:+.2f}); adjust energy labels.")
    if prof.get("clarity_avg") is not None:
        parts.append(f"- Clarity of task descriptions/instructions rated {prof['clarity_avg']:.1f}/5" + (f" (with AI instructions: {prof['clarity_with_instructions_avg']:.1f}/5)" if prof.get("clarity_with_instructions_avg") else "") + "; be more concrete and step-by-step if low.")
    if prof.get("difficulty_avg") is not None:
        parts.append(f"- Perceived difficulty averages {prof['difficulty_avg']:.1f}/5; break down tasks rated hard.")
    if prof.get("recent_notes"):
        parts.append("- Recent user notes: " + " | ".join(prof["recent_notes"][:3]))
    return "\n".join(parts)
