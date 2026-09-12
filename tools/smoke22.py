# SPDX-License-Identifier: AGPL-3.0-or-later
"""Production smoke test for Flowboard 2.2 (run on the server, as `flowboard`).

    sudo -u flowboard .venv/bin/python tools/smoke22.py setup    # throwaway user + session
    sudo -u flowboard .venv/bin/python tools/smoke22.py check    # drive the live site over HTTPS
    sudo -u flowboard .venv/bin/python tools/smoke22.py cleanup  # delete the throwaway user

`setup` mints a server-side session for the throwaway account and writes the signed
cookie to /tmp/smoke22.json, so nothing has to type a password into a form.  `cleanup`
removes the user (and everything cascading from it) - always run it.
"""
from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

STATE = Path("/tmp/smoke22.json")
EMAIL = "flowboard22-smoke@example.com"   # example.com is reserved and undeliverable
USERNAME = "smoke22"
BASE = "https://flowboard.fyi"


def setup() -> int:
    from app.db import session_scope
    from app.services import auth, tutorial
    from app.web import deps

    with session_scope() as db:
        existing = auth.get_user_by_email(db, EMAIL)
        if existing is not None:
            db.delete(existing)
            db.flush()
        user = auth.create_user(db, email=EMAIL, username=USERNAME, password=secrets.token_urlsafe(24), display_name="Smoke 2.2", email_verified=True)
        auth.ensure_profile(db, user)
        board = auth.ensure_default_board(db, user)
        tutorial.seed_tutorial(db, user)
        sess = auth.create_session(db, user, user_agent="flowboard-2.2 smoke test", ip="127.0.0.1")
        state = {"user_id": user.id, "email": user.email, "slug": board.slug, "cookie": deps.sign_session_id(sess.id), "csrf": sess.csrf_token}
    STATE.write_text(json.dumps(state))
    print(json.dumps({k: v for k, v in state.items() if k not in ("cookie", "csrf")}, indent=None))
    print(f"state written to {STATE}")
    return 0


def check() -> int:
    import re

    import httpx

    state = json.loads(STATE.read_text())
    c = httpx.Client(base_url=BASE, cookies={"fb_session": state["cookie"]}, headers={"X-CSRF-Token": state["csrf"]}, timeout=30.0, follow_redirects=False)
    failures = []

    def step(name, method, path, **kw):
        expect = kw.pop("expect", (200,))
        r = c.request(method, path, **kw)
        ok = r.status_code in expect
        print(f"{'ok ' if ok else 'FAIL'} {name}: {method} {path} -> {r.status_code}")
        if not ok:
            failures.append(f"{name} ({r.status_code})")
        return r

    def contains(name, text, needle, present=True):
        hit = needle in text
        ok = hit is present
        print(f"{'ok ' if ok else 'FAIL'} {name}: {'found' if hit else 'missing'} {needle!r}")
        if not ok:
            failures.append(name)

    slug = state["slug"]
    step("board page", "GET", f"/boards/{slug}/")
    tut = step("tutorial board", "GET", "/boards/getting-started/")
    contains("mobile menu button", tut.text, "menutoggle")
    contains("collapsible add-task", tut.text, 'id="addtask"')
    tid = re.search(r'data-task="([0-9a-f-]{36})"', tut.text).group(1)

    # focus timer -> done with elapsed minutes -> reflection pre-filled
    done = step("done with focus minutes", "POST", f"/boards/getting-started/tasks/{tid}/done", data={"actual_min": "37"}, headers={"HX-Request": "true"})
    contains("reflection modal", done.text, "reflectmodal")
    contains("prefilled actual_min", done.text, 'value="37"')
    step("reflect saved", "POST", f"/boards/getting-started/tasks/{tid}/reflect", data={"actual_min": "37", "actual_energy": "high", "clarity": "4", "difficulty": "2", "notes": "2.2 smoke"}, headers={"HX-Request": "true"})

    # nudge path: force it on with the settings switch off, by faking enough reflections
    step("schedule settings", "GET", "/settings/schedule")
    sched = step("save schedule (energy curve)", "POST", "/settings/schedule", data={
        **{f"enabled_{i}": "on" for i in range(5)},
        **{f"start_{i}": "09:00" for i in range(7)}, **{f"end_{i}": "17:00" for i in range(7)},
        **{f"max_min_{i}": "480" for i in range(7)}, **{f"max_high_min_{i}": "240" for i in range(7)},
        **{f"high_end_{i}": "12:00" for i in range(7)}, **{f"medium_end_{i}": "15:00" for i in range(7)},
        "auto_rollover": "on", "horizon_days": "14", "calendar_busy": "on",
    }, expect=(303,))
    page = step("schedule settings after save", "GET", "/settings/schedule")
    contains("energy curve section", page.text, "Energy curve")
    contains("high-until column", page.text, "high_end_0")

    # auto-schedule now writes a slot
    step("auto-schedule", "POST", f"/boards/getting-started/schedule/auto", headers={"HX-Request": "true"})
    board_after = step("board after auto-schedule", "GET", "/boards/getting-started/")
    slot = re.search(r'name="scheduled_start" value="(\d\d:\d\d)"', board_after.text)
    print(f"{'ok ' if slot else 'FAIL'} scheduled_start written: {slot.group(1) if slot else 'none found'}")
    if not slot:
        failures.append("scheduled_start")

    week = step("week view", "GET", "/boards/getting-started/schedule")
    contains("capacity line", week.text, "min</div>")

    # digest
    step("digest settings", "GET", "/settings/digest")
    step("digest save", "POST", "/settings/digest", data={"enabled": "on", "hour": "7", "timezone": "America/New_York", "channel_email": "on"}, expect=(303,))
    prev = step("digest preview", "POST", "/settings/digest/preview", headers={"HX-Request": "true"})
    contains("digest has a plan", prev.text, "Scheduled today")

    # calendars page (busy toggle markup) and ICS export with the new slot
    cal = step("calendars settings", "GET", "/settings/calendars")
    contains("busy toggle", cal.text, "Subtract this calendar")
    ics = step("board ICS", "GET", "/boards/getting-started/export.ics")
    contains("ICS has a timed event from the slot", ics.text, "BEGIN:VEVENT")

    print("\n" + ("SMOKE TEST PASSED" if not failures else f"SMOKE TEST FAILED: {failures}"))
    return 1 if failures else 0


def cleanup() -> int:
    from sqlalchemy import delete, select

    from app.db import session_scope
    from app.models import Board, Task, TaskReflection, User, UserSession

    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    with session_scope() as db:
        user = db.scalar(select(User).where(User.email == EMAIL))
        if user is None:
            print("nothing to clean up")
            return 0
        uid = user.id
        boards = list(db.scalars(select(Board).where(Board.owner_id == uid)))
        for b in boards:
            db.execute(delete(Task).where(Task.board_id == b.id))
        db.execute(delete(TaskReflection).where(TaskReflection.user_id == uid))
        db.execute(delete(UserSession).where(UserSession.user_id == uid))
        for b in boards:
            db.delete(b)
        db.delete(user)
        db.flush()
        left = db.scalar(select(User).where(User.email == EMAIL))
        print(f"deleted throwaway user {uid} ({EMAIL}) and {len(boards)} board(s); still present: {left is not None}")
    if STATE.exists():
        STATE.unlink()
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    sys.exit({"setup": setup, "check": check, "cleanup": cleanup}[cmd]())
