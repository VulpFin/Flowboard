# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Login / registration / logout / password reset / TG11 OIDC."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from ...config import settings
from ...db import get_db
from ...identity import tg11_api
from ...identity.oidc import PROVIDER_ID, OIDCClient, OIDCError
from ...models import User
from ...services import auth as auth_service
from .. import deps

router = APIRouter(tags=["auth"])
_flow_signer = URLSafeTimedSerializer(settings.FLOWBOARD_SECRET_KEY, salt="oidc-flow")
FLOW_COOKIE = "fb_oidc_flow"


def _safe_next(url: Optional[str]) -> str:
    if not url or not url.startswith("/") or url.startswith("//"):
        return "/"
    return url


def _login_user(request: Request, db: Session, user: User, next_url: str, auth_method: str = "password") -> RedirectResponse:
    sess = auth_service.create_session(db, user, user_agent=request.headers.get("user-agent", ""), ip=request.client.host if request.client else "", auth_method=auth_method)
    resp = deps.redirect(_safe_next(next_url))
    deps.set_session_cookie(resp, sess.id)
    resp.delete_cookie(deps.CSRF_COOKIE, path="/")
    return resp


@router.get("/login")
def login_page(request: Request, next: str = "/", user: Optional[User] = Depends(deps.get_current_user_optional)):
    if user is not None:
        return deps.redirect(_safe_next(next))
    return deps.render(request, "auth/login.html", {"next": _safe_next(next)})


@router.post("/login", dependencies=[Depends(deps.csrf_protect)])
def login_submit(request: Request, identifier: str = Form(...), password: str = Form(...), next: str = Form("/"), db: Session = Depends(get_db)):
    if not settings.TG11_LOCAL_LOGIN_ENABLED:
        return deps.render(request, "auth/login.html", {"next": _safe_next(next), "error": "Local login is disabled. Use TG11 sign-in."}, status_code=403)
    user = auth_service.authenticate(db, identifier, password)
    if user is None:
        return deps.render(request, "auth/login.html", {"next": _safe_next(next), "error": "Invalid email/username or password.", "identifier": identifier}, status_code=401)
    return _login_user(request, db, user, next)


@router.get("/register")
def register_page(request: Request, user: Optional[User] = Depends(deps.get_current_user_optional)):
    if user is not None:
        return deps.redirect("/")
    if not settings.FLOWBOARD_ALLOW_REGISTRATION:
        return deps.render(request, "auth/login.html", {"next": "/", "error": "Registration is closed."}, status_code=403)
    return deps.render(request, "auth/register.html")


@router.post("/register", dependencies=[Depends(deps.csrf_protect)])
def register_submit(request: Request, email: str = Form(...), username: str = Form(...), password: str = Form(...), password2: str = Form(...), display_name: str = Form(""), db: Session = Depends(get_db)):
    if not settings.FLOWBOARD_ALLOW_REGISTRATION:
        return deps.redirect("/login?err=Registration+is+closed")
    if password != password2:
        return deps.render(request, "auth/register.html", {"error": "Passwords do not match.", "email": email, "username": username, "display_name": display_name}, status_code=400)
    try:
        user = auth_service.create_user(db, email=email, username=username, password=password, display_name=display_name)
    except auth_service.AuthError as exc:
        return deps.render(request, "auth/register.html", {"error": str(exc), "email": email, "username": username, "display_name": display_name}, status_code=400)
    return _login_user(request, db, user, "/")


@router.post("/logout", dependencies=[Depends(deps.csrf_protect)])
def logout(request: Request, db: Session = Depends(get_db)):
    sid = deps.read_session_id(request)
    if sid:
        auth_service.revoke_session(db, sid)
    resp = deps.redirect("/login?msg=Signed+out")
    deps.clear_session_cookie(resp)
    return resp


# --- password reset -----------------------------------------------------

@router.get("/password/forgot")
def forgot_page(request: Request):
    return deps.render(request, "auth/forgot.html")


@router.post("/password/forgot", dependencies=[Depends(deps.csrf_protect)])
def forgot_submit(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    from ...services.mail import send_password_reset

    user = auth_service.get_user_by_email(db, email)
    if user is not None and user.is_active:
        token = auth_service.create_password_reset(db, user)
        send_password_reset(user, settings.absolute_url(f"/password/reset/{token}"))
    return deps.render(request, "auth/forgot.html", {"sent": True})


@router.get("/password/reset/{token}")
def reset_page(request: Request, token: str):
    return deps.render(request, "auth/reset.html", {"token": token})


@router.post("/password/reset/{token}", dependencies=[Depends(deps.csrf_protect)])
def reset_submit(request: Request, token: str, password: str = Form(...), password2: str = Form(...), db: Session = Depends(get_db)):
    if password != password2:
        return deps.render(request, "auth/reset.html", {"token": token, "error": "Passwords do not match."}, status_code=400)
    user = auth_service.consume_password_reset(db, token)
    if user is None:
        return deps.render(request, "auth/reset.html", {"token": token, "error": "This reset link is invalid or has expired."}, status_code=400)
    try:
        auth_service.set_password(db, user, password)
    except auth_service.AuthError as exc:
        return deps.render(request, "auth/reset.html", {"token": token, "error": str(exc)}, status_code=400)
    return deps.redirect("/login?msg=Password+updated.+Please+sign+in.")


# --- TG11 OIDC -----------------------------------------------------------

@router.get("/auth/tg11/login")
def tg11_login(request: Request, next: str = "/", link: int = 0, sync: int = 0, push: str = ""):
    if not settings.oidc_configured:
        return deps.redirect("/login?err=TG11+sign-in+is+not+configured")
    try:
        client = OIDCClient.from_settings()
        flow = client.new_flow_state()
        flow["next"] = _safe_next(next)
        flow["link"] = "1" if link else "0"
        flow["sync"] = "1" if sync else "0"
        flow["push"] = push[:400] if push else ""  # comma-separated provider ids, or "*" for all
        url = client.authorization_url(flow)
    except OIDCError as exc:
        return deps.redirect(f"/login?err=TG11+sign-in+unavailable:+{exc}")
    resp = deps.redirect(url)
    resp.set_cookie(FLOW_COOKIE, _flow_signer.dumps(flow), max_age=600, httponly=True, secure=not settings.is_dev, samesite="lax", path="/auth/tg11")
    return resp


@router.get("/auth/tg11/callback")
def tg11_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None, error_description: Optional[str] = None, db: Session = Depends(get_db), current: Optional[User] = Depends(deps.get_current_user_optional)):
    if error:
        return deps.redirect(f"/login?err=TG11:+{error_description or error}")
    raw = request.cookies.get(FLOW_COOKIE)
    try:
        flow = _flow_signer.loads(raw or "", max_age=600)
    except BadSignature:
        return deps.redirect("/login?err=Sign-in+session+expired,+try+again")
    if not code or not state or state != flow.get("state"):
        return deps.redirect("/login?err=Invalid+sign-in+state")
    try:
        claims = OIDCClient.from_settings().exchange(code, flow)
    except OIDCError as exc:
        return deps.redirect(f"/login?err=TG11+sign-in+failed:+{exc}")
    def _import_keys(u: User) -> str:
        if "tg11.ai" not in (claims.scope or "").split() or not claims.access_token:
            return ""
        try:
            res = tg11_api.import_vault(db, u, tg11_api.fetch_vault(claims.access_token))
        except Exception:
            return ""
        return f"+({len(res['imported'])}+AI+key(s)+synced+from+TG11)" if res["imported"] else ""

    try:
        if flow.get("push") and current is not None:
            if current.tg11_user_id and current.tg11_user_id != claims.subject:
                return deps.redirect("/settings/ai-providers?err=Signed+in+to+a+different+TG11+account+than+the+one+linked+here")
            if "tg11.ai" not in (claims.scope or "").split():
                return deps.redirect("/settings/ai-providers?err=TG11+did+not+grant+the+tg11.ai+permission")
            sel = None if flow["push"] == "*" else [p for p in flow["push"].split(",") if p]
            res = tg11_api.push_selected(db, current, claims.access_token, sel)
            resp = deps.redirect("/settings/ai-providers?msg=Pushed+to+TG11:+" + "+".join(res["written"]) + ("+(skipped:+" + "+".join(res["skipped"]) + ")" if res.get("skipped") else "") if res.get("ok") else f"/settings/ai-providers?err=Push+failed:+{res.get('error')}")
            resp.delete_cookie(FLOW_COOKIE, path="/auth/tg11")
            return resp
        if flow.get("sync") == "1" and current is not None:
            if current.tg11_user_id and current.tg11_user_id != claims.subject:
                return deps.redirect("/settings/ai-providers?err=Signed+in+to+a+different+TG11+account+than+the+one+linked+here")
            note = _import_keys(current)
            resp = deps.redirect("/settings/ai-providers?msg=Synced+from+TG11" + note if note else "/settings/ai-providers?msg=Nothing+to+sync:+your+TG11+vault+has+no+keys+Flowboard+can+use+(or+the+tg11.ai+scope+is+not+granted)")
            resp.delete_cookie(FLOW_COOKIE, path="/auth/tg11")
            return resp
        if flow.get("link") == "1" and current is not None:
            # explicit account linking from Settings -> Security
            from ...models import IdentityLink, utcnow
            from sqlalchemy import select

            existing = db.scalar(select(IdentityLink).where(IdentityLink.provider == PROVIDER_ID, IdentityLink.subject == claims.subject))
            if existing is not None and existing.user_id != current.id:
                return deps.redirect("/settings/security?err=That+TG11+identity+is+already+linked+to+another+Flowboard+account")
            if existing is None:
                db.add(IdentityLink(user_id=current.id, provider=PROVIDER_ID, issuer=settings.TG11_OIDC_ISSUER, subject=claims.subject, email_at_link=claims.email, username_at_link=claims.preferred_username, migration_source="account_link", migration_status="linked", last_login_at=utcnow()))
                current.tg11_user_id = claims.subject
                tg11_api.register_link(claims.subject, current.id, source="account_link")
            note = _import_keys(current)
            resp = deps.redirect("/settings/security?msg=TG11+account+linked" + note)
            resp.delete_cookie(FLOW_COOKIE, path="/auth/tg11")
            return resp
        user, _created = auth_service.get_or_create_user_for_identity(db, provider=PROVIDER_ID, issuer=settings.TG11_OIDC_ISSUER, subject=claims.subject, email=claims.email, email_verified=claims.email_verified, preferred_username=claims.preferred_username, display_name=claims.name)
        if _created:
            tg11_api.register_link(claims.subject, user.id)
        _import_keys(user)
    except auth_service.AuthError as exc:
        return deps.redirect(f"/login?err={str(exc)}")
    from ...models import utcnow as _now
    user.last_login_at = _now()
    resp = _login_user(request, db, user, flow.get("next", "/"), auth_method="tg11")
    resp.delete_cookie(FLOW_COOKIE, path="/auth/tg11")
    return resp
