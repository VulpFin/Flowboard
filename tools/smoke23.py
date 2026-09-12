# SPDX-License-Identifier: AGPL-3.0-or-later
"""Production smoke test for Flowboard 2.3 (run on the server, as `flowboard`).

    sudo -u flowboard .venv/bin/python tools/smoke23.py run       # setup, check, clean up
    sudo -u flowboard .venv/bin/python tools/smoke23.py cleanup   # in case a run died half way

Two throwaway accounts that each own a board called "personal": one invites the
other, and the qualified reference has to keep them apart.  Invitations are
created through the service layer rather than the HTTP endpoint so the test
never sends mail to a reserved domain; every other step goes over HTTPS against
the live site.
"""
from __future__ import annotations

import json
import re
import secrets
import sys

import httpx

BASE = "https://flowboard.fyi"
EMAILS = ["flowboard23-a@example.com", "flowboard23-b@example.com"]
failures: list = []


def step(ok: bool, label: str, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def _client(state):
    return httpx.Client(base_url=BASE, cookies={"fb_session": state["cookie"]},
                        headers={"X-CSRF-Token": state["csrf"]}, timeout=30.0, follow_redirects=False)


def setup():
    from app.db import session_scope
    from app.models import BoardRole
    from app.services import auth, boards as bs, invites as inv
    from app.web import deps

    out = {}
    with session_scope() as db:
        users = []
        for i, email in enumerate(EMAILS):
            u = auth.get_user_by_email(db, email)
            if u is not None:
                db.delete(u)
                db.flush()
            u = auth.create_user(db, email=email, username=f"smoke23{'ab'[i]}", password=secrets.token_urlsafe(24), display_name=f"Smoke {'AB'[i]}", email_verified=True)
            auth.ensure_profile(db, u)
            auth.ensure_default_board(db, u)
            users.append(u)
        a, b = users
        board_a = bs.get_board_for_user(db, a, "personal")
        board_b = bs.get_board_for_user(db, b, "personal")
        invite, token = inv.create(db, board_a, a, b.email, BoardRole.EDITOR, message="smoke test")
        for u, key in ((a, "a"), (b, "b")):
            sess = auth.create_session(db, u, user_agent="flowboard 2.3 smoke", ip="127.0.0.1")
            out[key] = {"cookie": deps.sign_session_id(sess.id), "csrf": sess.csrf_token, "id": u.id, "email": u.email, "username": u.username}
        out["token"] = token
        out["invite_id"] = invite.id
        out["board_a"] = board_a.id
        out["board_b"] = board_b.id
        out["ref"] = f"{a.username}~personal"
    return out


def check(state):
    a, b = _client(state["a"]), _client(state["b"])
    ref = state["ref"]

    r = b.get(f"/invites/{state['token']}")
    step(r.status_code == 200 and "Accept invitation" in r.text, "invited user sees the landing page")
    step("smoke test" in r.text, "the invitation note is shown")
    r = a.get(f"/invites/{state['token']}")
    step(r.status_code == 200 and "you are signed in as" in r.text, "the inviter cannot redeem it")

    r = b.get("/invites")
    step(r.status_code == 200 and "Accept" in r.text, "pending invitations page lists it")
    r = b.get("/boards")
    step("✉" in r.text, "the nav badge shows a waiting invitation")

    r = b.post(f"/invites/{state['token']}/accept")
    step(r.status_code == 303 and ref in r.headers.get("location", ""), "accepting redirects to the qualified board", r.headers.get("location", ""))

    r = b.get(f"/boards/{ref}/")
    step(r.status_code == 200, "the shared board opens at owner~slug")
    step(f"/boards/{ref}/tasks" in r.text, "its forms post to the qualified reference")
    own = b.get("/boards/personal/")
    step(own.status_code == 200 and f"/boards/{ref}/tasks" not in own.text, "their own /boards/personal/ is still their own board")

    r = b.post(f"/boards/{ref}/tasks", data={"title": "Added by the invited member"}, headers={"HX-Request": "true"})
    step(r.status_code == 200 and "Added by the invited member" in r.text, "an editor can add tasks on the shared board")
    tid = (re.findall(r'data-task="([0-9a-f-]{36})"', r.text) or [""])[-1]
    r = b.post(f"/boards/{ref}/tasks/{tid}/assign", data={"assigned_to_id": state["b"]["id"]}, headers={"HX-Request": "true"})
    step(r.status_code == 200 and "👤" in r.text, "the task can be assigned to them")

    r = a.get(f"/boards/personal/schedule")
    step(r.status_code == 200 and "Who has room this week" in r.text, "the owner sees the member capacity panel")
    step(state["b"]["username"] in r.text.lower() or "Smoke B" in r.text, "the member is listed there")

    r = a.get("/boards/personal/settings")
    step(r.status_code == 200 and "Members" in r.text and "Remove" in r.text, "members panel renders for the owner")
    r = b.get(f"/boards/{ref}/settings")
    step(r.status_code == 403, "an editor cannot open board settings")

    r = a.post(f"/boards/personal/members/{state['b']['id']}/role", data={"role": "viewer"})
    step(r.status_code == 303, "the owner can change a role")
    r = b.post(f"/boards/{ref}/tasks", data={"title": "should be refused"}, headers={"HX-Request": "true"})
    step(r.status_code == 403, "a viewer cannot add tasks")

    r = a.post(f"/boards/personal/members/{state['b']['id']}/remove")
    step(r.status_code == 303, "the owner can remove a member")
    r = b.get(f"/boards/{ref}/")
    step(r.status_code == 404, "the board is gone for them afterwards", str(r.status_code))


def cleanup():
    from sqlalchemy import delete, select

    from app.db import session_scope
    from app.models import Board, BoardInvite, BoardMembership, Task, TaskReflection, User, UserSession

    with session_scope() as db:
        for email in EMAILS:
            user = db.scalar(select(User).where(User.email == email))
            if user is None:
                continue
            boards = list(db.scalars(select(Board).where(Board.owner_id == user.id)))
            for bo in boards:
                db.execute(delete(Task).where(Task.board_id == bo.id))
                db.execute(delete(BoardInvite).where(BoardInvite.board_id == bo.id))
                db.execute(delete(BoardMembership).where(BoardMembership.board_id == bo.id))
            db.execute(delete(BoardMembership).where(BoardMembership.user_id == user.id))
            db.execute(delete(TaskReflection).where(TaskReflection.user_id == user.id))
            db.execute(delete(UserSession).where(UserSession.user_id == user.id))
            for bo in boards:
                db.delete(bo)
            db.delete(user)
            db.flush()
        left = [e for e in EMAILS if db.scalar(select(User).where(User.email == e)) is not None]
        print(f"cleanup done; throwaway accounts still present: {left or 'none'}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "cleanup":
        cleanup()
        sys.exit(0)
    state = setup()
    print(json.dumps({k: v for k, v in state.items() if k not in ("a", "b", "token")}))
    try:
        check(state)
    finally:
        cleanup()
    print("\n" + ("SMOKE TEST PASSED" if not failures else f"SMOKE TEST FAILED: {failures}"))
    sys.exit(1 if failures else 0)
