# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Board pages: home redirect, board list, board view, create/edit/archive."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import Board, User
from ...services import auth as auth_service
from ...services import boards as board_service
from ...services import schedule as schedule_service
from ...services import tasks as task_service
from ...services import tutorial as tutorial_service
from ...ai.assistant import QUICK_PROMPTS
from ...ai.client import resolve_model
from ...calendars import links as cal_links
from .. import deps

router = APIRouter(tags=["boards"])


@router.get("/")
def home(user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    board = auth_service.ensure_default_board(db, user)
    if user.profile is not None and not user.profile.tutorial_seeded:  # accounts created before 2.1
        try:
            tutorial_service.seed_tutorial(db, user)
        except Exception:
            pass
    return deps.redirect(f"/boards/{board_service.ref_for(board, user)}/")


@router.get("/boards")
def board_list(request: Request, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    boards = board_service.list_boards_for_user(db, user, include_archived=True)
    counts, my_role = {}, {}
    for b in boards:
        counts[b.id] = task_service.board_stats(task_service.list_tasks(db, b, include_done=True))
        m = board_service.membership_for(db, b, user)
        my_role[b.id] = m.role if m is not None else ""
    return deps.render(request, "boards/list.html", {"all_boards": boards, "counts": counts, "my_role": my_role, "default_board_id": user.profile.default_board_id if user.profile else None}, db=db)


@router.post("/boards", dependencies=[Depends(deps.csrf_protect)])
def board_create(request: Request, name: str = Form(...), description: str = Form(""), icon: str = Form(""), color: str = Form(""), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    try:
        board = board_service.create_board(db, user, name=name, description=description, icon=icon, color=color, settings=json.loads(user.profile.board_defaults_json or "{}") if user.profile else {})
    except ValueError as exc:
        return deps.redirect(f"/boards?err={exc}")
    return deps.redirect(f"/boards/{board_service.ref_for(board, user)}/?msg=Board+created")


@router.get("/boards/{slug}/")
def board_view(request: Request, board: Board = Depends(deps.current_board), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    membership = board_service.membership_for(db, board, user)
    if membership and membership.role_enum.can_edit:
        try:
            schedule_service.rollover(db, user, board)  # lazy: unfinished work from past days moves forward
        except Exception:
            pass
    tasks = task_service.list_tasks(db, board, include_done=False)
    columns = task_service.as_columns(tasks)
    all_tasks = task_service.list_tasks(db, board, include_done=True)
    model = resolve_model(db, user, board)
    members = board_service.member_list(db, board)
    return deps.render(request, "boards/view.html", {
        "columns": columns, "stats": task_service.board_stats(all_tasks), "ai_model": model.ref if model else None,
        "quick_prompts": QUICK_PROMPTS, "can_edit": membership.role_enum.can_edit if membership else False,
        "cal_links": cal_links.links_for_board(db, board), "connections": cal_links.list_connections(db, user),
        "board_settings": board_service.board_settings(board),
        "members": members, "member_names": {m["id"]: m["name"] for m in members},
    }, db=db)


@router.get("/boards/{slug}/settings")
def board_settings_page(request: Request, board: Board = Depends(deps.current_board_admin), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    from ...ai.client import available_models
    from ...calendars import feeds
    from ...services import invites as invite_service

    feed = feeds.get_feed(db, user, board)
    return deps.render(request, "boards/settings.html", {
        "bsettings": board_service.board_settings(board), "model_groups": available_models(db, user), "feed": feed,
        "members": board_service.member_list(db, board), "invites": invite_service.for_board(db, board),
        "roles": [r.value for r in invite_service.INVITABLE_ROLES],
    }, db=db)


@router.post("/boards/{slug}/settings", dependencies=[Depends(deps.csrf_protect)])
def board_settings_save(request: Request, name: str = Form(...), description: str = Form(""), icon: str = Form(""), color: str = Form(""), ai_model: str = Form(""), default_context: str = Form(""), regenerate_slug: Optional[str] = Form(None), board: Board = Depends(deps.current_board_admin), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    st = board_service.board_settings(board)
    st["ai_model"] = ai_model.strip()
    st["default_context"] = default_context.strip()[:32]
    board_service.update_board(db, board, name=name, description=description, icon=icon, color=color, settings=st, regenerate_slug=bool(regenerate_slug))
    return deps.redirect(f"/boards/{board_service.ref_for(board, user)}/settings?msg=Saved")


@router.post("/boards/{slug}/archive", dependencies=[Depends(deps.csrf_protect)])
def board_archive(board: Board = Depends(deps.current_board_admin), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    board_service.update_board(db, board, is_archived=not board.is_archived)
    if board.is_archived and user.profile and user.profile.default_board_id == board.id:
        user.profile.default_board_id = None
        auth_service.ensure_default_board(db, user)
    return deps.redirect("/boards?msg=" + ("Board+archived" if board.is_archived else "Board+restored"))


@router.post("/boards/{slug}/delete", dependencies=[Depends(deps.csrf_protect)])
def board_delete(confirm: str = Form(""), board: Board = Depends(deps.current_board_admin), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    if confirm.strip() != board.slug:
        return deps.redirect(f"/boards/{board_service.ref_for(board, user)}/settings?err=Type+the+board+slug+to+confirm+deletion")
    if board.owner_id != user.id:
        return deps.redirect(f"/boards/{board_service.ref_for(board, user)}/settings?err=Only+the+owner+can+delete+a+board")
    board_service.delete_board(db, board)
    if user.profile and user.profile.default_board_id == board.id:
        user.profile.default_board_id = None
    auth_service.ensure_default_board(db, user)
    return deps.redirect("/boards?msg=Board+deleted")


@router.post("/boards/{slug}/make-default", dependencies=[Depends(deps.csrf_protect)])
def board_make_default(board: Board = Depends(deps.current_board), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    user.profile.default_board_id = board.id
    return deps.redirect(f"/boards?msg={board.name}+is+now+your+default+board")


@router.get("/boards/{slug}/schedule")
def board_schedule(request: Request, start: Optional[str] = None, days: int = 7, board: Board = Depends(deps.current_board), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    from datetime import date, timedelta

    try:
        start_d = date.fromisoformat(start) if start else date.today()
    except ValueError:
        start_d = date.today()
    days = max(1, min(days, 28))
    membership = board_service.membership_for(db, board, user)
    can_edit = membership.role_enum.can_edit if membership else False
    if can_edit:
        try:
            schedule_service.rollover(db, user, board)
        except Exception:
            pass
    all_boards = board_service.list_boards_for_user(db, user)
    plan = schedule_service.day_plan(db, user, all_boards, start_d, days)
    unscheduled = [t for t in task_service.list_tasks(db, board, include_done=False) if not t.scheduled_date]
    board_names = {b.id: b for b in all_boards}
    members = board_service.member_list(db, board)
    # shared board: how much room each member has, in minutes only - never their
    # other boards' task titles
    team = []
    if len(members) > 1:
        for m in members:
            team.append({"name": m["name"], "role": m["role"], "id": m["id"], "days": schedule_service.free_minutes_by_day(db, m["user"], start_d, min(days, 7))})
    return deps.render(request, "boards/schedule.html", {
        "plan": plan, "unscheduled": unscheduled, "start": start_d, "days": days, "can_edit": can_edit, "board_names": board_names,
        "prev": (start_d - timedelta(days=days)).isoformat(), "next": (start_d + timedelta(days=days)).isoformat(), "today": date.today(),
        "sched": schedule_service.load_schedule(user), "weekdays": schedule_service.WEEKDAYS,
        "members": members, "member_names": {m["id"]: m["name"] for m in members}, "team": team,
    }, db=db)


# --- members & invitations --------------------------------------------------

@router.post("/boards/{slug}/members/invite", dependencies=[Depends(deps.csrf_protect)])
def member_invite(email: str = Form(...), role: str = Form("editor"), message: str = Form(""), board: Board = Depends(deps.current_board_admin), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    """Invite someone by email; they do not need an account yet."""
    from ...models import BoardRole
    from ...services import invites as invite_service

    ref = board_service.ref_for(board, user)
    try:
        invite, token = invite_service.create(db, board, user, email, BoardRole(role.strip().lower()), message=message)
    except (invite_service.InviteError, ValueError) as exc:
        return deps.redirect(f"/boards/{ref}/settings?err={exc}")
    sent = invite_service.send(invite, token, board, user)
    task_service.log_activity(db, board, actor_id=user.id, action="invited", summary=f"Invited {invite.email} as {invite.role}")
    if sent:
        return deps.redirect(f"/boards/{ref}/settings?msg=Invitation+sent+to+{invite.email}")
    # SMTP not configured / refused: show the link so it can be passed on by hand
    return deps.redirect(f"/boards/{ref}/settings?err=Could+not+send+the+email.+Share+this+link+instead:+{invite_service.invite_url(token)}")


@router.post("/boards/{slug}/invites/{invite_id}/revoke", dependencies=[Depends(deps.csrf_protect)])
def invite_revoke(invite_id: str, board: Board = Depends(deps.current_board_admin), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    from ...services import invites as invite_service

    inv = invite_service.get_for_board(db, board, invite_id)
    if inv is not None:
        invite_service.revoke(db, inv)
    return deps.redirect(f"/boards/{board_service.ref_for(board, user)}/settings?msg=Invitation+revoked")


@router.post("/boards/{slug}/members/{member_id}/role", dependencies=[Depends(deps.csrf_protect)])
def member_role(member_id: str, role: str = Form(...), board: Board = Depends(deps.current_board_admin), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    from ...models import BoardRole

    ref = board_service.ref_for(board, user)
    try:
        board_service.set_member_role(db, board, member_id, BoardRole(role.strip().lower()))
    except (ValueError, KeyError) as exc:
        return deps.redirect(f"/boards/{ref}/settings?err={exc}")
    return deps.redirect(f"/boards/{ref}/settings?msg=Role+updated")


@router.post("/boards/{slug}/members/{member_id}/remove", dependencies=[Depends(deps.csrf_protect)])
def member_remove(member_id: str, board: Board = Depends(deps.current_board_admin), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    ref = board_service.ref_for(board, user)
    try:
        board_service.remove_member(db, board, member_id)
    except ValueError as exc:
        return deps.redirect(f"/boards/{ref}/settings?err={exc}")
    task_service.log_activity(db, board, actor_id=user.id, action="member_removed", summary="Removed a member; their tasks here were unassigned")
    return deps.redirect(f"/boards/{ref}/settings?msg=Member+removed")


@router.post("/boards/{slug}/members/leave", dependencies=[Depends(deps.csrf_protect)])
def member_leave(board: Board = Depends(deps.current_board), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    """Leave a board someone else owns."""
    try:
        board_service.remove_member(db, board, user.id)
    except ValueError as exc:
        return deps.redirect(f"/boards/{board_service.ref_for(board, user)}/settings?err={exc}")
    auth_service.ensure_default_board(db, user)
    return deps.redirect(f"/boards?msg=You+left+{board.name}")


@router.get("/invites")
def invites_page(request: Request, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    from ...services import invites as invite_service

    return deps.render(request, "boards/invites.html", {"invites": invite_service.for_user(db, user)}, db=db)


@router.get("/invites/{token}")
def invite_landing(request: Request, token: str, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    from ...services import invites as invite_service

    inv = invite_service.resolve(db, token)
    if inv is None:
        return deps.render(request, "boards/invite.html", {"invite": None, "token": token, "problem": "That invitation link is not valid."}, db=db, status_code=404)
    problem = None
    if not inv.is_open:
        problem = f"That invitation is {inv.state}."
    elif invite_service.normalize_email(user.email) != inv.email:
        problem = f"That invitation was sent to {inv.email}, but you are signed in as {user.email}."
    return deps.render(request, "boards/invite.html", {"invite": inv, "token": token, "problem": problem}, db=db)


@router.post("/invites/id/{invite_id}/{decision}", dependencies=[Depends(deps.csrf_protect)])
def invite_decide_by_id(invite_id: str, decision: str, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    """Accept/decline from the /invites list: being signed in as the invited
    address is the same proof the emailed token carries."""
    from ...models import BoardInvite
    from ...services import invites as invite_service

    inv = db.get(BoardInvite, invite_id)
    if inv is None or invite_service.normalize_email(user.email) != inv.email:
        return deps.redirect("/invites?err=That+invitation+is+not+yours")
    return _decide(db, inv, user, decision)


@router.post("/invites/{token}/{decision}", dependencies=[Depends(deps.csrf_protect)])
def invite_decide(token: str, decision: str, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    from ...services import invites as invite_service

    inv = invite_service.resolve(db, token)
    if inv is None:
        return deps.redirect("/invites?err=That+invitation+link+is+not+valid")
    return _decide(db, inv, user, decision)


def _decide(db: Session, inv, user: User, decision: str):
    from ...services import invites as invite_service

    try:
        if decision == "accept":
            invite_service.accept(db, inv, user)
            board = db.get(Board, inv.board_id)
            task_service.log_activity(db, board, actor_id=user.id, action="member_joined", summary=f"{user.display_name or user.username} joined as {inv.role}")
            return deps.redirect(f"/boards/{board_service.ref_for(board, user)}/?msg=Welcome+to+{board.name}")
        if decision == "decline":
            invite_service.decline(db, inv, user)
            return deps.redirect("/invites?msg=Invitation+declined")
    except invite_service.InviteError as exc:
        return deps.redirect(f"/invites?err={exc}")
    return deps.redirect("/invites")


@router.post("/tutorial/recreate", dependencies=[Depends(deps.csrf_protect)])
def tutorial_recreate(user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    board = tutorial_service.seed_tutorial(db, user, force=True)
    return deps.redirect(f"/boards/{board_service.ref_for(board, user)}/?msg=Tutorial+board+created")


@router.get("/boards/{slug}/activity")
def board_activity(request: Request, board: Board = Depends(deps.current_board), db: Session = Depends(get_db)):
    return deps.render(request, "boards/activity.html", {"entries": task_service.recent_activity(db, board, limit=200)}, db=db)
