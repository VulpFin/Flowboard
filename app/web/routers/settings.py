# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Account-wide settings: account, profile, AI defaults, security, board defaults."""
from __future__ import annotations

import json
from datetime import date
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
from ...services import reflections as reflection_service
from ...services import schedule as schedule_service
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


@router.get("/schedule")
def schedule_page(request: Request, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    sched = schedule_service.load_schedule(user)
    learned = reflection_service.learned_curve(user)
    declared = {i: schedule_service.declared_blocks(sched["days"][str(i)]) for i in range(7)}
    learned_preview = schedule_service.learned_blocks(learned.get("levels") or {}, sched["days"][str(date.today().weekday())]) if learned else []
    return deps.render(request, "settings/schedule.html", {
        "section": "schedule", "sched": sched, "weekdays": schedule_service.WEEKDAYS,
        "calibration": reflection_service.get_profile(user), "calibration_text": reflection_service.prompt_summary(user),
        "declared_curve": {i: schedule_service.curve_text(b) for i, b in declared.items()},
        "learned": learned, "learned_curve_text": schedule_service.curve_text(learned_preview),
        "curve_min_samples": reflection_service.CURVE_MIN_SAMPLES, "nudge_after": reflection_service.NUDGE_AFTER,
    }, db=db)


@router.post("/schedule", dependencies=[Depends(deps.csrf_protect)])
async def schedule_save(request: Request, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    form = await request.form()
    schedule_service.save_schedule(db, user, {k: str(v) for k, v in form.items()})
    return deps.redirect("/settings/schedule?msg=Schedule+saved")


@router.post("/schedule/reset-calibration", dependencies=[Depends(deps.csrf_protect)])
def calibration_reset(user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    from sqlalchemy import delete
    from ...models import TaskReflection

    db.execute(delete(TaskReflection).where(TaskReflection.user_id == user.id))
    user.profile.calibration_json = "{}"
    return deps.redirect("/settings/schedule?msg=Calibration+data+cleared")


@router.get("/digest")
def digest_page(request: Request, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    from ...ai.client import resolve_model
    from ...services import digest as digest_service

    model = resolve_model(db, user)
    return deps.render(request, "settings/digest.html", {
        "section": "digest", "digest": digest_service.load(user), "all_boards": board_service.list_boards_for_user(db, user),
        "channels": list(digest_service.CHANNELS), "ai_model": model.ref if model else None,
        "smtp_configured": bool(app_settings.SMTP_HOST), "timezone": user.profile.timezone if user.profile else "UTC",
    }, db=db)


@router.post("/digest", dependencies=[Depends(deps.csrf_protect)])
async def digest_save(request: Request, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    from ...services import digest as digest_service

    form = await request.form()
    tz = (form.get("timezone") or "").strip()[:64]
    if tz:
        user.profile.timezone = tz
    digest_service.save(db, user, form, all_board_ids=[b.id for b in board_service.list_boards_for_user(db, user)])
    return deps.redirect("/settings/digest?msg=Digest+settings+saved")


@router.post("/digest/preview", dependencies=[Depends(deps.csrf_protect)])
def digest_preview(request: Request, user: User = Depends(deps.get_current_user), db: Session = Depends(get_db)):
    """Render today's digest without sending or marking it sent."""
    from ...services import digest as digest_service

    built = digest_service.build(db, user)
    if built is None:
        subject, body = "Nothing to send today", "“Only when something is due” is on and nothing is scheduled, due or overdue today."
    else:
        subject, body = built[0], built[1]
    return deps.render(request, "settings/_digest_preview.html", {"subject": subject, "body": body})


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
