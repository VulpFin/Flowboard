# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Account-wide settings: account, profile, AI defaults, security, board defaults."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...ai.client import available_models
from ...config import settings as app_settings
from ...db import get_db
from ...models import IdentityLink, User, UserSession
from ...services import auth as auth_service
from ...services import boards as board_service
from .. import deps

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
def settings_home():
    return deps.redirect("/settings/account")


@router.get("/account")
def account_page(request: Request, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    return deps.render(request, "settings/account.html", {"section": "account", "all_boards": board_service.list_boards_for_user(db, user)}, db=db)


@router.post("/account", dependencies=[Depends(deps.csrf_protect)])
def account_save(request: Request, display_name: str = Form(""), email: str = Form(...), username: str = Form(...), default_board_id: str = Form(""), timezone: str = Form("UTC"), work_day_minutes: int = Form(480), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    email_n = auth_service.normalize_email(email)
    username_n = auth_service.normalize_username(username)
    if not auth_service.EMAIL_RE.match(email_n):
        return deps.redirect("/settings/account?err=Invalid+email")
    if not auth_service.USERNAME_RE.match(username_n):
        return deps.redirect("/settings/account?err=Invalid+username")
    other = auth_service.get_user_by_email(db, email_n)
    if other is not None and other.id != user.id:
        return deps.redirect("/settings/account?err=Email+already+in+use")
    other = auth_service.get_user_by_username(db, username_n)
    if other is not None and other.id != user.id:
        return deps.redirect("/settings/account?err=Username+already+taken")
    if email_n != user.email:
        user.email_verified_at = None
    user.email, user.username, user.display_name = email_n, username_n, display_name.strip()[:80] or username_n
    p = user.profile
    p.timezone = timezone.strip()[:64] or "UTC"
    p.work_day_minutes = max(15, min(int(work_day_minutes or 480), 1440))
    if default_board_id:
        try:
            b = board_service.get_board_for_user(db, user, default_board_id)
            p.default_board_id = b.id
        except Exception:
            pass
    return deps.redirect("/settings/account?msg=Saved")


@router.get("/ai-defaults")
def ai_defaults_page(request: Request, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    return deps.render(request, "settings/ai_defaults.html", {"section": "ai-defaults", "model_groups": available_models(db, user)}, db=db)


@router.post("/ai-defaults", dependencies=[Depends(deps.csrf_protect)])
def ai_defaults_save(default_ai_model: str = Form(""), ai_fallback_enabled: Optional[str] = Form(None), ai_fallback_model: str = Form(""), ai_auto_tag_on_create: Optional[str] = Form(None), ai_usage_tracking: Optional[str] = Form(None), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    p = user.profile
    p.default_ai_model = default_ai_model.strip()[:200]
    p.ai_fallback_enabled = bool(ai_fallback_enabled)
    p.ai_fallback_model = ai_fallback_model.strip()[:200]
    p.ai_auto_tag_on_create = bool(ai_auto_tag_on_create)
    p.ai_usage_tracking = bool(ai_usage_tracking)
    return deps.redirect("/settings/ai-defaults?msg=Saved")


@router.get("/security")
def security_page(request: Request, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    sessions = list(db.scalars(select(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None)).order_by(UserSession.last_seen_at.desc())))
    links = list(db.scalars(select(IdentityLink).where(IdentityLink.user_id == user.id)))
    current_sid = deps.read_session_id(request)
    return deps.render(request, "settings/security.html", {"section": "security", "sessions": sessions, "links": links, "current_sid": current_sid, "oidc_configured": app_settings.oidc_configured}, db=db)


@router.post("/security/password", dependencies=[Depends(deps.csrf_protect)])
def change_password(request: Request, current_password: str = Form(""), new_password: str = Form(...), new_password2: str = Form(...), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    if user.has_password and not auth_service.verify_password_for(user, current_password):
        return deps.redirect("/settings/security?err=Current+password+is+incorrect")
    if new_password != new_password2:
        return deps.redirect("/settings/security?err=New+passwords+do+not+match")
    try:
        auth_service.set_password(db, user, new_password)
    except auth_service.AuthError as exc:
        return deps.redirect(f"/settings/security?err={exc}")
    # keep the current session alive
    sid = deps.read_session_id(request)
    sess = db.get(UserSession, sid) if sid else None
    if sess is not None:
        sess.revoked_at = None
    return deps.redirect("/settings/security?msg=Password+changed;+other+sessions+signed+out")


@router.post("/security/sessions/revoke-others", dependencies=[Depends(deps.csrf_protect)])
def revoke_others(request: Request, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    n = auth_service.revoke_other_sessions(db, user, deps.read_session_id(request) or "")
    return deps.redirect(f"/settings/security?msg=Signed+out+{n}+other+session(s)")


@router.post("/security/sessions/{session_id}/revoke", dependencies=[Depends(deps.csrf_protect)])
def revoke_one(session_id: str, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    sess = db.get(UserSession, session_id)
    if sess is not None and sess.user_id == user.id:
        auth_service.revoke_session(db, session_id)
    return deps.redirect("/settings/security?msg=Session+revoked")


@router.post("/security/links/{link_id}/unlink", dependencies=[Depends(deps.csrf_protect)])
def unlink_identity(link_id: str, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    link = db.get(IdentityLink, link_id)
    if link is None or link.user_id != user.id:
        return deps.redirect("/settings/security?err=Link+not+found")
    if not user.has_password:
        return deps.redirect("/settings/security?err=Set+a+password+first+so+you+can+still+sign+in")
    db.delete(link)
    if link.provider == "tg11":
        user.tg11_user_id = None
    return deps.redirect("/settings/security?msg=Identity+unlinked")


@router.get("/board-defaults")
def board_defaults_page(request: Request, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    try:
        defaults = json.loads(user.profile.board_defaults_json or "{}")
    except Exception:
        defaults = {}
    return deps.render(request, "settings/board_defaults.html", {"section": "board-defaults", "defaults": defaults, "model_groups": available_models(db, user)}, db=db)


@router.post("/board-defaults", dependencies=[Depends(deps.csrf_protect)])
def board_defaults_save(ai_model: str = Form(""), default_context: str = Form(""), default_estimate_min: int = Form(30), user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    user.profile.board_defaults_json = json.dumps({"ai_model": ai_model.strip(), "default_context": default_context.strip()[:32], "default_estimate_min": max(1, min(int(default_estimate_min or 30), 1440))})
    return deps.redirect("/settings/board-defaults?msg=Saved")
