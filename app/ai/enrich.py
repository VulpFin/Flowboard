# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Task enrichment (contexts / estimate / importance / energy) - port of the
pre-2.0 `ai_tagging` module on top of the provider abstraction."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import Board, Task, User
from .client import AIClient
from .types import ChatMessage, ProviderError

ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "contexts": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
        "estimate_min": {"type": "integer", "minimum": 1, "maximum": 1440},
        "importance": {"type": "integer", "minimum": 1, "maximum": 5},
        "energy": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["contexts"],
}

CONTEXT_HINT = "Excel, Email, Coding, Docs, Slides, Calls, Errand, Design, Meetings, Lab, Warehouse, Office, General"


def _neighbors(tasks: List[Task], title: str, limit: int = 8) -> List[Dict[str, Any]]:
    base = {w.lower() for w in title.split() if len(w) >= 4}
    scored = []
    for t in tasks:
        overlap = 1.0 if base & {w.lower() for w in t.title.split()} else 0.0
        scored.append(((2.0 if not t.done else 1.2) + 0.8 * overlap, t))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"title": t.title, "ctx": t.primary_context, "estimate_min": t.estimate_min, "importance": t.importance, "energy": t.energy, "done": t.done, "actual_min": t.last_actual_min}
        for _, t in scored[:limit]
    ]


def bounded_adjust(suggested: Optional[int], current: int, neighbor_avg: Optional[float]) -> int:
    if not isinstance(suggested, int) or suggested < 1 or suggested > 1440:
        return current
    low, high = int(round(current * 0.8)), int(round(current * 1.2))
    if neighbor_avg:
        baseline = int(round((current + neighbor_avg) / 2))
        low = max(5, min(low, int(round(baseline * 0.7))))
        high = min(1440, max(high, int(round(baseline * 1.3))))
    return max(low, min(high, suggested))


def enrich(db: Session, user: User, board: Board, title: str, description: str, all_tasks: List[Task], *, fields: bool = True) -> Dict[str, Any]:
    """Return {"ok": bool, "contexts": [...], "estimate_min": int|None, ...}.
    Never raises for provider problems - returns ok=False with `error`."""
    client = AIClient(db, user, board)
    if client.resolved is None:
        return {"ok": False, "error": "no AI provider configured"}
    neighbors = _neighbors(all_tasks, title)
    prompt = (
        f"Enrich this task for a planner.\nTitle: {title}\nDetails: {description}\n\n"
        f"Nearby & past tasks: {json.dumps(neighbors, ensure_ascii=False)}\n\n"
        f"Infer 1-3 short contexts (prefer: {CONTEXT_HINT}). "
        + ("Also infer estimate_min (1-1440), importance (1-5) and energy (low/medium/high). "
           "If similar tasks were completed faster than estimated, bias estimates downward." if fields else "Return only contexts.")
    )
    try:
        data = client.structured(
            [ChatMessage(role="system", content="You label and size tasks. Respond only with the requested JSON."), ChatMessage(role="user", content=prompt)],
            schema=ENRICH_SCHEMA,
            name="enrich_task",
            description="Return contexts and sizing for the task.",
            max_tokens=200,
            operation="enrich",
        )
    except ProviderError as exc:
        return {"ok": False, "error": exc.friendly()}
    vals = [n.get("actual_min") or n.get("estimate_min") for n in neighbors if isinstance(n.get("actual_min") or n.get("estimate_min"), int)]
    return {
        "ok": True,
        "contexts": [str(c).strip().title()[:32] for c in data.get("contexts", []) if str(c).strip()][:3] or ["General"],
        "estimate_min": data.get("estimate_min") if fields else None,
        "importance": data.get("importance") if fields else None,
        "energy": data.get("energy") if fields else None,
        "neighbor_avg": (sum(vals) / len(vals)) if vals else None,
    }
