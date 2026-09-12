# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Calendar routes: OAuth connect/callback/disconnect, target calendar,
push task to calendar, subscription feeds."""
from __future__ import annotations

import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from ...calendars import feeds, ics
from ...calendars import links as cal_links
from ...calendars.providers import CALENDAR_PROVIDERS, CalendarProviderError, get_provider, pkce_pair
from ...config import settings
from ...db import get_db
from ...models import Board, User
from ...services import boards as board_service
from ...services import tasks as task_service
from .. import deps

router = APIRouter(tags=["calendars"])
_flow = URLSafeTimedSerializer(settings.FLOWBOARD_SECRET_KEY, salt="cal-oauth")
FLOW_COOKIE = "fb_cal_flow"


@router.get("/settings/calendars")
def calendars_page(request: Request, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    conns = cal_links.list_connections(db, user)
    providers = [{"id": pid, "name": cls.name, "configured": cls().configured} for pid, cls in CALENDAR_PROVIDERS.items()]
    board_feeds = []
    for b in board_service.list_boards_for_user(db, user):
        board_feeds.append({"board": b, "feed": feeds.get_feed(db, user, b)})
    return deps.render(request, "settings/calendars.html", {"section": "calendars", "connections": conns, "providers": providers, "board_feeds": board_feeds, "cached": {c.id: cal_links.cached_calendars(c) for c in conns}}, db=db)


# --- OAuth connect ----------------------------------------------------------

@router.get("/calendar/connect/{provider}")
def connect_start(provider: str, user: User = Depends(deps.get_current_user)):
    try:
        prov = get_provider(provider)
    except CalendarProviderError:
        raise HTTPException(404, "Unknown calendar provider")
    if not prov.configured:
        return deps.redirect(f"/settings/calendars?err={prov.name}+is+not+configured+on+this+server")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    resp = deps.redirect(prov.authorize_url(state, challenge))
    resp.set_cookie(FLOW_COOKIE, _flow.dumps({"state": state, "verifier": verifier, "provider": provider, "user": user.id}), max_age=600, httponly=True, secure=not settings.is_dev, samesite="lax", path="/calendar/connect")
    return resp


@router.get("/calendar/connect/{provider}/callback")
def connect_callback(request: Request, provider: str, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    if error:
        return deps.redirect(f"/settings/calendars?err={error}")
    try:
        flow = _flow.loads(request.cookies.get(FLOW_COOKIE, ""), max_age=600)
    except BadSignature:
        return deps.redirect("/settings/calendars?err=Connection+attempt+expired")
    if flow.get("user") != user.id or flow.get("provider") != provider or not code or state != flow.get("state"):
        return deps.redirect("/settings/calendars?err=Invalid+OAuth+state")
    prov = get_provider(provider)
    try:
        token = prov.exchange_code(code, flow["verifier"])
        account = prov.account_info(token)
        conn = cal_links.store_connection(db, user, provider, token, account, prov.scopes)
        cal_links.refresh_calendar_list(db, conn)
    except CalendarProviderError as exc:
        return deps.redirect(f"/settings/calendars?err={exc}")
    resp = deps.redirect(f"/settings/calendars?msg={prov.name}+connected+({account.get('email', '')})")
    resp.delete_cookie(FLOW_COOKIE, path="/calendar/connect")
    return resp


@router.post("/calendar/connections/{conn_id}/target", dependencies=[Depends(deps.csrf_protect)])
def set_target(conn_id: str, calendar_id: str = Form(...), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    conn = cal_links.get_connection(db, user, conn_id)
    if conn is None:
        raise HTTPException(404, "Connection not found")
    try:
        cal_links.set_target_calendar(db, conn, calendar_id)
    except ValueError:
        return deps.redirect("/settings/calendars?err=Unknown+calendar")
    return deps.redirect("/settings/calendars?msg=Target+calendar+saved")


@router.post("/calendar/connections/{conn_id}/refresh", dependencies=[Depends(deps.csrf_protect)])
def refresh_conn(conn_id: str, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    conn = cal_links.get_connection(db, user, conn_id)
    if conn is None:
        raise HTTPException(404, "Connection not found")
    try:
        cal_links.refresh_calendar_list(db, conn)
        conn.status, conn.status_message = "connected", ""
    except CalendarProviderError as exc:
        return deps.redirect(f"/settings/calendars?err={exc}")
    return deps.redirect("/settings/calendars?msg=Calendar+list+refreshed")


@router.post("/calendar/connections/{conn_id}/busy", dependencies=[Depends(deps.csrf_protect)])
def set_busy(conn_id: str, busy_enabled: Optional[str] = Form(None), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    """Whether this calendar's events reduce the daily scheduling capacity."""
    conn = cal_links.get_connection(db, user, conn_id)
    if conn is None:
        raise HTTPException(404, "Connection not found")
    conn.busy_enabled = bool(busy_enabled)
    conn.busy_cache_json, conn.busy_fetched_at = "{}", None  # drop the cache either way
    db.flush()
    return deps.redirect("/settings/calendars?msg=" + ("Meetings+from+this+calendar+now+reduce+your+daily+capacity" if conn.busy_enabled else "This+calendar+no+longer+affects+your+capacity"))


@router.post("/calendar/connections/{conn_id}/disconnect", dependencies=[Depends(deps.csrf_protect)])
def disconnect(conn_id: str, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    conn = cal_links.get_connection(db, user, conn_id)
    if conn is None:
        raise HTTPException(404, "Connection not found")
    cal_links.disconnect(db, conn)
    if user.profile and user.profile.calendar_default_connection_id == conn_id:
        user.profile.calendar_default_connection_id = None
    return deps.redirect("/settings/calendars?msg=Disconnected.+Events+already+in+your+calendar+were+left+in+place.")


# --- push a task ------------------------------------------------------------

@router.post("/boards/{slug}/tasks/{task_id}/calendar", dependencies=[Depends(deps.csrf_protect)])
def push_task(request: Request, task_id: str, connection_id: str = Form(...), board: Board = Depends(deps.current_board_editor), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    t = task_service.get_task(db, board, task_id)
    conn = cal_links.get_connection(db, user, connection_id)
    if t is None or conn is None:
        raise HTTPException(404, "Task or connection not found")
    err = None
    try:
        cal_links.push_task(db, user, board, t, conn, timezone=user.profile.timezone if user.profile else "UTC")
    except (CalendarProviderError, ValueError) as exc:
        err = str(exc)
    from .tasks import _board_partial

    return _board_partial(request, db, board, {"ai_error": err})


@router.post("/boards/{slug}/tasks/{task_id}/calendar/{link_id}/unlink", dependencies=[Depends(deps.csrf_protect)])
def unlink_task(request: Request, task_id: str, link_id: str, delete_remote: Optional[str] = Form(None), board: Board = Depends(deps.current_board_editor), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    from ...models import CalendarEventLink

    link = db.get(CalendarEventLink, link_id)
    if link is None or link.user_id != user.id or link.board_id != board.id or link.task_id != task_service.get_task(db, board, task_id).id:
        raise HTTPException(404, "Link not found")
    cal_links.unlink(db, user, link, delete_remote=bool(delete_remote))
    from .tasks import _board_partial

    return _board_partial(request, db, board)


# --- subscription feeds ------------------------------------------------------

@router.post("/boards/{slug}/feed/rotate", dependencies=[Depends(deps.csrf_protect)])
def feed_rotate(request: Request, include_done: Optional[str] = Form(None), board: Board = Depends(deps.current_board), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    row, token = feeds.create_or_rotate_feed(db, user, board, include_done=bool(include_done))
    return deps.render(request, "settings/_feed_created.html", {"board": board, "feed": row, "url": feeds.feed_url(token), "webcal": feeds.webcal_url(token)}, db=db)


@router.post("/boards/{slug}/feed/revoke", dependencies=[Depends(deps.csrf_protect)])
def feed_revoke(board: Board = Depends(deps.current_board), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    row = feeds.get_feed(db, user, board)
    if row is not None:
        feeds.revoke_feed(db, row)
    return deps.redirect("/settings/calendars?msg=Feed+revoked")


@router.get("/calendar/feed/{token}.ics")
def feed(token: str, db: Session = Depends(get_db)):
    """Unauthenticated by design (calendar apps cannot log in); the 256-bit
    token *is* the credential."""
    row = feeds.resolve_feed_token(db, token)
    if row is None:
        raise HTTPException(404, "Feed not found")
    board = db.get(Board, row.board_id)
    if board is None:
        raise HTTPException(404, "Feed not found")
    tasks = task_service.list_tasks(db, board, include_done=True)
    body = ics.board_ics(tasks, board, include_done=row.include_done)
    return Response(body, media_type="text/calendar; charset=utf-8", headers={"Cache-Control": "private, max-age=300", "Content-Disposition": f'inline; filename="flowboard-{board.slug}.ics"'})
