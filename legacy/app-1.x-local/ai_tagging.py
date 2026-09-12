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

import os, json, re
from typing import List, Optional, Dict, Any
from openai import OpenAI
from dotenv import load_dotenv
from . import ai_log


load_dotenv()

# --- lazy OpenAI client (so the app can boot without a key) ---
_client = None

# Configure client to read API key from env; optionally include org/project.
def _get_client():
    """Create and cache the OpenAI client on first use."""
    global _client
    if _client is not None:
        return _client
    # Import lazily so missing SDKs don't break app startup
    from openai import OpenAI

    kwargs = {}
    org = os.getenv("OPENAI_ORG_ID")
    proj = os.getenv("OPENAI_PROJECT_ID")
    if org:
        kwargs["organization"] = org
    if proj:
        kwargs["project"] = proj
    _client = OpenAI(**kwargs)
    return _client


# --- shared constants/helpers ---
VALID_ENERGY = {"low", "medium", "high"}

SYSTEM_PROMPT_CONTEXTS = (
    "You label tasks with 1-3 short contexts that minimize setup switching. "
    "Examples: Excel, Email, Coding, Docs, Slides, Calls, Errand, Design, Meetings, Lab, Warehouse, Office, General. "
    "Return a JSON array of strings (contexts)."
)


def _extract_json_object(text: str) -> Optional[dict]:
    """Pull the first JSON object from a messy string (handles ``` fences)."""
    if not text:
        return None
    s = text.strip()
    # strip code fences if present
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE)
    m = re.search(r"\{.*\}", s, flags=re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# --- simple contexts only ---
def llm_contexts(title: str, description: str) -> List[str]:
    """Return ['General'] on failure; otherwise 1–3 contexts."""
    if not os.getenv("OPENAI_API_KEY"):
        return ["General"]
    content = f"Task: {title}\nDetails: {description}\nReturn only JSON array."
    req = {
        "op": "contexts",
        "title": title,
        "description": description,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT_CONTEXTS},
            {"role": "user", "content": content},
        ],
    }
    try:
        client = _get_client()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=req["messages"],
            temperature=0.0,
            max_tokens=64,
        )
        raw = (resp.choices[0].message.content or "").strip()
        ai_log.write({**req, "status": "ok", "response": raw})
        # Try array first; if not, try to extract from object with key "contexts"
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                arr = data
            elif isinstance(data, dict) and isinstance(data.get("contexts"), list):
                arr = data["contexts"]
            else:
                arr = None
        except Exception:
            obj = _extract_json_object(raw)
            arr = obj.get("contexts") if isinstance(obj, dict) else None

        if isinstance(arr, list):
            out = [str(x).strip().title()[:32] for x in arr if str(x).strip()]
            return out[:3] or ["General"]
    except Exception as e:
        ai_log.write({**req, "status": "error", "error": repr(e)})
        print("[flowboard.ai] contexts error:", repr(e))
        return ["General"]
    return ["General"]


# --- full enrichment: contexts + estimate_min + importance + energy ---
def llm_enrich(title: str, description: str):
    """
    Returns: {"ok": bool, "contexts": list|None, "estimate_min": int|None,
              "importance": int|None, "energy": str|None}
    On failure: ok=False with all fields None (caller should not overwrite).
    """
    if not os.getenv("OPENAI_API_KEY"):
        return {
            "ok": False,
            "contexts": None,
            "estimate_min": None,
            "importance": None,
            "energy": None,
        }

    prompt = f"""You are enriching a task for a planner.
Task title: {title}
Details: {description}

Infer:
- 1–3 short contexts (choose from: Excel, Email, Coding, Docs, Slides, Calls, Errand, Design, Meetings, Lab, Warehouse, Office, General)
- estimated minutes (1..1440)
- importance (1..5, 5 is critical)
- energy: low, medium, or high

Return ONLY JSON:
{{"contexts":["..."], "estimate_min": <int>, "importance": <int>, "energy": "<low|medium|high>"}}"""
    req = {
        "op": "enrich",
        "title": title,
        "description": description,
        "messages": [
            {
                "role": "system",
                "content": "Respond ONLY with a single JSON object; no commentary.",
            },
            {"role": "user", "content": prompt},
        ],
    }

    try:
        client = _get_client()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=req["messages"],
            temperature=0.0,
            max_tokens=160,
        )
        raw = (resp.choices[0].message.content or "").strip()
        ai_log.write({**req, "status": "ok", "response": raw})
        data = _extract_json_object(raw)
        if not isinstance(data, dict):
            print("[flowboard.ai] parse-fail; raw:", raw[:200])
            ai_log.write({**req, "status": "parse-fail", "response": raw})
            return {"ok": False, "contexts": None, "estimate_min": None, "importance": None, "energy": None}
    except Exception as e:
        ai_log.write({**req, "status": "error", "error": repr(e)})
        print("[flowboard.ai] openai error:", repr(e))
        return {"ok": False, "contexts": None, "estimate_min": None, "importance": None, "energy": None}

    # Normalize + guardrails
    contexts = data.get("contexts")
    if isinstance(contexts, list):
        contexts = [str(x).strip().title()[:32] for x in contexts if str(x).strip()][:3]
        if not contexts:
            contexts = None
    else:
        contexts = None

    def _opt_int(v, lo, hi):
        try:
            x = int(v)
            return max(lo, min(hi, x))
        except Exception:
            return None

    estimate_min = _opt_int(data.get("estimate_min"), 1, 1440)
    importance = _opt_int(data.get("importance"), 1, 5)

    energy = str(data.get("energy", "")).strip().lower()
    if energy not in VALID_ENERGY:
        energy = None

    return {
        "ok": True,
        "contexts": contexts,
        "estimate_min": estimate_min,
        "importance": importance,
        "energy": energy,
    }


def _neighbor_stats(neighbors):
    """Return (count, count_with_actuals, avg_actual_or_est)."""
    vals = []
    for n in neighbors:
        v = n.get("actual_min") or n.get("estimate_min")
        if isinstance(v, int) and 1 <= v <= 1440:
            vals.append(v)
    if not vals:
        return 0, 0, None
    # prefer actuals when present
    actuals = [
        n["actual_min"] for n in neighbors if isinstance(n.get("actual_min"), int)
    ]
    avg_actual = sum(actuals) / len(actuals) if actuals else None
    avg_any = sum(vals) / len(vals)
    return len(vals), len(actuals), (avg_actual or avg_any)


def _bounded_adjust(suggested: int, current: int, neighbor_avg: int | None) -> int:
    """
    Keep changes sane:
      - If no neighbors, allow ±20% max.
      - If neighbors exist, allow within a band around both current and neighbor_avg.
      - Never drop below 5 minutes, never exceed 24h.
    """
    if not isinstance(suggested, int) or suggested < 1 or suggested > 1440:
        return current

    # Default bounds if no evidence
    low = int(round(current * 0.8))
    high = int(round(current * 1.2))

    if neighbor_avg:
        # Blend current and neighbor baseline; widen band only with evidence.
        baseline = int(round((current + neighbor_avg) / 2))
        # permit some exploration but clamp extremes
        low = max(5, min(low, int(round(baseline * 0.7))))
        high = min(1440, max(high, int(round(baseline * 1.3))))

    # Clamp the suggested value into [low, high]
    return max(low, min(high, suggested))


def _select_neighbors(all_tasks: Dict[str, "Task"], target_title: str, limit: int = 8):
    """
    Pick related tasks: same primary context or title keywords; include done ones too.
    Returns a compact list of dicts safe for prompting.
    """

    def _primary_ctx(t):
        return t.contexts[0] if t.contexts else "General"

    tasks = list(all_tasks.values())

    # quick keyword bag
    base = set(w.lower() for w in target_title.split() if len(w) >= 4)

    scored = []
    for t in tasks:
        # similarity by context + title overlap
        ctx_score = 1.0 if base & set(w.lower() for w in t.title.split()) else 0.0
        scored.append(((2.0 if not t.done else 1.2) + (0.8 if ctx_score else 0.0), t))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for _, t in scored[:limit]:
        out.append(
            {
                "title": t.title,
                "ctx": (t.contexts[0] if t.contexts else "General"),
                "estimate_min": t.estimate_min,
                "importance": t.importance,
                "energy": t.energy,
                "done": t.done,
                "actual_min": t.last_actual_min,
            }
        )
    return out


def llm_enrich_contextual(title: str, description: str, all_tasks: Dict[str, "Task"]):
    """
    Context-aware enrichment using neighbors (siblings + history).
    Returns same shape as llm_enrich(): {"ok":bool, fields...}
    """
    if not os.getenv("OPENAI_API_KEY"):
        return {
            "ok": False,
            "contexts": None,
            "estimate_min": None,
            "importance": None,
            "energy": None,
        }

    neighbors = _select_neighbors(all_tasks, title, limit=8)
    prompt = f"""You're enriching a task with awareness of surrounding work and history.

Task:
- title: {title}
- details: {description}

Nearby & past tasks (recent first, may include completed):
{json.dumps(neighbors, ensure_ascii=False)}

Heuristics you SHOULD apply:
- If earlier tasks include account setup, subsequent similar tasks take LESS time.
- If assets (e.g., edited videos) now exist, reduce estimate for follow-ups.
- If many are done quickly (actual_min << estimate_min), bias estimates downward.
- Keep importance 1..5 (5=critical). Energy is low/medium/high.

Return ONLY JSON:
{{"contexts":["..."], "estimate_min": <int>, "importance": <int>, "energy": "<low|medium|high>"}}"""

    try:
        client = _get_client()
        req_msgs = [
            {
                "role": "system",
                "content": "Respond ONLY with a single JSON object; no commentary.",
            },
            {"role": "user", "content": prompt},
        ]
        req = {
            "op": "enrich_ctx",
            "title": title,
            "description": description,
            "neighbors": neighbors,
            "messages": req_msgs,
        }
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=req_msgs,
            temperature=0.0,
            max_tokens=200,
        )
        raw = (resp.choices[0].message.content or "").strip()
        ai_log.write({**req, "status": "ok", "response": raw})
        data = _extract_json_object(raw)
        if not isinstance(data, dict):
            print("[flowboard.ai] ctx-parse-fail; raw:", raw[:200])
            ai_log.write({**req, "status": "parse-fail", "response": raw})
            return {
                "ok": False,
                "contexts": None,
                "estimate_min": None,
                "importance": None,
                "energy": None,
            }
    except Exception as e:
        print("[flowboard.ai] ctx-openai error:", repr(e))
        ai_log.write({**req, "status": "error", "error": repr(e)})
        return {
            "ok": False,
            "contexts": None,
            "estimate_min": None,
            "importance": None,
            "energy": None,
        }

    # same normalization as llm_enrich
    contexts = data.get("contexts")
    if isinstance(contexts, list):
        contexts = [str(x).strip().title()[:32] for x in contexts if str(x).strip()][
            :3
        ] or None
    else:
        contexts = None

    def _opt_int(v, lo, hi):
        try:
            x = int(v)
            return max(lo, min(hi, x))
        except:
            return None

    raw_est = _opt_int(data.get("estimate_min"), 1, 1440)
    raw_imp = _opt_int(data.get("importance"), 1, 5)
    raw_eng = (
        str(data.get("energy", "")).strip().lower()
        if str(data.get("energy", "")).strip().lower() in {"low", "medium", "high"}
        else None
    )

    count_any, count_actuals, neighbor_avg = _neighbor_stats(neighbors)
    # We don't know the task's "current" estimate here, so the caller passes it in;
    # when adding a new task we fall back to 30 if missing.
    # We'll return both raw and neighbor info so the caller can adjust with the current value.

    return {
        "ok": True,
        "contexts": contexts,
        "estimate_min": raw_est,
        "importance": raw_imp,
        "energy": raw_eng,
        "neighbor_avg": neighbor_avg,
        "evidence": {"count": count_any, "actuals": count_actuals},
    }
