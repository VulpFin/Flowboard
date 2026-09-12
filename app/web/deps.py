# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""FastAPI dependencies: DB session, current user, CSRF, board resolution,
templates."""
from __future__ import annotations

import os
import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from .. import __version__
from ..config import settings
from ..db import get_db
from ..models import Board, BoardRole, User, UserSession
from ..services import auth as auth_service
from ..services.boards import BoardAccessDenied, BoardNotFound, get_board_for_user, list_boards_for_user

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals.update(site_name=settings.FLOWBOARD_SITE_NAME, site_url=settings.FLOWBOARD_SITE_URL, app_version=__version__, oidc_enabled=settings.oidc_configured, oidc_label=settings.TG11_OIDC_LOGIN_LABEL, local_login_enabled=settings.TG11_LOCAL_LOGIN_ENABLED, registration_enabled=settings.FLOWBOARD_ALLOW_REGISTRATION)

_signer = URLSafeTimedSerializer(settings.FLOWBOARD_SECRET_KEY, salt="fb-session")
CSRF_COOKIE = "fb_csrf"


class LoginRequired(Exception):
    def __init__(self, next_url: str = "/"):
        self.next_url = next_url


# --- session cookie helpers -------------------------------------------

def sign_session_id(session_id: str) -> str:
    return _signer.dumps(session_id)


def read_session_id(request: Request) -> Optional[str]:
    raw = request.cookies.get(settings.FLOWBOARD_SESSION_COOKIE)
    if not raw:
        return None
    try:
        return _signer.loads(raw, max_age=settings.FLOWBOARD_SESSION_MAX_AGE)
    except BadSignature:
        return None


def set_session_cookie(response, session_id: str) -> None:
    response.set_cookie(
        settings.FLOWBOARD_SESSION_COOKIE,
        sign_session_id(session_id),
        max_age=settings.FLOWBOARD_SESSION_MAX_AGE,
        httponly=True,
        secure=settings.FLOWBOARD_COOKIE_SECURE and not settings.is_dev,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(settings.FLOWBOARD_SESSION_COOKIE, path="/")


# --- dependencies -----------------------------------------------------

def get_session(request: Request, db: Session = Depends(get_db)) -> Optional[UserSession]:
    sid = read_session_id(request)
    if not sid:
        return None
    sess = auth_service.get_valid_session(db, sid)
    if sess is not None:
        request.state.session = sess
    return sess


def get_current_user_optional(request: Request, db: Session = Depends(get_db), sess: Optional[UserSession] = Depends(get_session)) -> Optional[User]:
    if sess is None:
        return None
    user = db.get(User, sess.user_id)
    if user is None or not user.is_active:
        return None
    request.state.user = user
    auth_service.ensure_profile(db, user)
    return user


def get_current_user(request: Request, user: Optional[User] = Depends(get_current_user_optional)) -> User:
    if user is None:
        raise LoginRequired(next_url=str(request.url.path))
    return user


def csrf_token_for(request: Request) -> str:
    """Token to embed in forms: the session's token if logged in, otherwise a
    double-submit cookie token (created on demand)."""
    sess = getattr(request.state, "session", None)
    if sess is not None:
        return sess.csrf_token
    tok = request.cookies.get(CSRF_COOKIE)
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.state.new_csrf_cookie = tok
    return tok


async def csrf_protect(request: Request, sess: Optional[UserSession] = Depends(get_session)) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    supplied = request.headers.get("X-CSRF-Token")
    if not supplied:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("application/x-www-form-urlencoded") or ctype.startswith("multipart/form-data"):
            form = await request.form()
            supplied = form.get("csrf_token")
    expected = sess.csrf_token if sess is not None else request.cookies.get(CSRF_COOKIE)
    if not supplied or not expected or not secrets.compare_digest(str(supplied), str(expected)):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
    # Origin check as second layer
    origin = request.headers.get("origin") or ""
    if origin and settings.trusted_origins and origin.rstrip("/") not in settings.trusted_origins and not settings.is_dev:
        raise HTTPException(status_code=403, detail="Untrusted origin")


def current_board(slug: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Board:
    try:
        board = get_board_for_user(db, user, slug)
    except BoardNotFound:
        raise HTTPException(status_code=404, detail="Board not found")
    except BoardAccessDenied:
        raise HTTPException(status_code=403, detail="No access to this board")
    request.state.board = board
    return board


def current_board_editor(slug: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Board:
    try:
        board = get_board_for_user(db, user, slug, require=BoardRole.EDITOR)
    except BoardNotFound:
        raise HTTPException(status_code=404, detail="Board not found")
    except BoardAccessDenied:
        raise HTTPException(status_code=403, detail="You can only view this board")
    request.state.board = board
    return board


def current_board_admin(slug: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Board:
    try:
        board = get_board_for_user(db, user, slug, require=BoardRole.ADMIN)
    except BoardNotFound:
        raise HTTPException(status_code=404, detail="Board not found")
    except BoardAccessDenied:
        raise HTTPException(status_code=403, detail="Only board admins can do this")
    request.state.board = board
    return board


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def render(request: Request, template: str, ctx: Optional[dict] = None, status_code: int = 200, db: Optional[Session] = None):
    """Template rendering with the common context (user, boards, csrf)."""
    context = {"request": request, "csrf_token": csrf_token_for(request)}
    user = getattr(request.state, "user", None)
    context["user"] = user
    if user is not None and db is not None:
        context["boards"] = list_boards_for_user(db, user)
    context["board"] = getattr(request.state, "board", None)
    context["msg"] = request.query_params.get("msg")
    context["err"] = request.query_params.get("err")
    context.update(ctx or {})
    resp = templates.TemplateResponse(request, template, context, status_code=status_code)
    new_tok = getattr(request.state, "new_csrf_cookie", None)
    if new_tok:
        resp.set_cookie(CSRF_COOKIE, new_tok, httponly=True, secure=settings.FLOWBOARD_COOKIE_SECURE and not settings.is_dev, samesite="lax", path="/", max_age=60 * 60 * 24)
    return resp


def redirect(url: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url, status_code=status_code)
