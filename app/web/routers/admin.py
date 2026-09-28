# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Staff-only account administration.

This deliberately exposes account state and identity-link metadata, never
password hashes, sessions, OAuth tokens, calendar credentials, or AI keys.
"""
from __future__ import annotations

import logging
import ipaddress
from datetime import timedelta
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import AIProviderCredential, AdminAuditEvent, Board, IPBlock, IdentityLink, User, UserSession, utcnow
from ...services import audit as audit_service
from ...services import auth as auth_service
from ...services import ip_blocks
from .. import deps

log = logging.getLogger("flowboard.admin")

router = APIRouter(prefix="/admin", tags=["admin"])


def _user_redirect(user_id: str, *, message: str, error: bool = False):
    key = "err" if error else "msg"
    return deps.redirect(f"/admin/users/{user_id}?{key}={quote_plus(message)}")


def _network_redirect(*, message: str, error: bool = False):
    key = "err" if error else "msg"
    return deps.redirect(f"/admin/network?{key}={quote_plus(message)}")


def _has_fresh_purge_reauth(request: Request, db: Session, user: User, target_id: str) -> bool:
    sid = deps.read_session_id(request)
    session = db.get(UserSession, sid) if sid else None
    return bool(
        session
        and session.user_id == user.id
        and session.reauth_target_id == target_id
        and session.reauthenticated_at
        and session.reauthenticated_at >= utcnow() - timedelta(minutes=15)
    )


@router.get("")
def admin_home(
    request: Request,
    query: str = "",
    user: User = Depends(deps.get_current_staff),
    db: Session = Depends(get_db),
):
    term = query.strip()[:100]
    statement = select(User).order_by(User.created_at.desc()).limit(100)
    if term:
        pattern = f"%{term}%"
        statement = statement.where(or_(User.email.ilike(pattern), User.username.ilike(pattern), User.display_name.ilike(pattern)))
    users = list(db.scalars(statement))
    totals = {
        "users": db.scalar(select(func.count()).select_from(User).where(User.deleted_at.is_(None))) or 0,
        "active": db.scalar(select(func.count()).select_from(User).where(User.is_active.is_(True), User.deleted_at.is_(None))) or 0,
        "staff": db.scalar(select(func.count()).select_from(User).where(User.is_staff.is_(True), User.is_active.is_(True))) or 0,
        "recoverable": db.scalar(select(func.count()).select_from(User).where(User.deleted_at.is_not(None))) or 0,
    }
    return deps.render(request, "admin/index.html", {"users": users, "query": term, "totals": totals, "section": "admin"}, db=db)


@router.get("/users/{user_id}")
def admin_user_detail(
    user_id: str,
    request: Request,
    user: User = Depends(deps.get_current_staff),
    db: Session = Depends(get_db),
):
    account = db.get(User, user_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    identity_links = list(db.scalars(select(IdentityLink).where(IdentityLink.user_id == account.id).order_by(IdentityLink.linked_at.desc())))
    board_count = db.scalar(select(func.count()).select_from(Board).where(Board.owner_id == account.id, Board.deleted_at.is_(None))) or 0
    deleted_boards = list(db.scalars(select(Board).where(Board.owner_id == account.id, Board.deleted_at.is_not(None)).order_by(Board.deleted_at.desc())))
    deleted_credentials = list(db.scalars(select(AIProviderCredential).where(AIProviderCredential.user_id == account.id, AIProviderCredential.deleted_at.is_not(None)).order_by(AIProviderCredential.deleted_at.desc())))
    audit_events = list(db.scalars(select(AdminAuditEvent).where(AdminAuditEvent.subject_type == "user", AdminAuditEvent.subject_id == account.id).order_by(AdminAuditEvent.created_at.desc()).limit(20)))
    return deps.render(request, "admin/user.html", {
        "account": account,
        "identity_links": identity_links,
        "board_count": board_count,
        "deleted_boards": deleted_boards,
        "deleted_credentials": deleted_credentials,
        "audit_events": audit_events,
        "section": "admin",
        "is_self": account.id == user.id,
        "can_permanently_delete": account.deleted_at is not None and _has_fresh_purge_reauth(request, db, user, account.id),
    }, db=db)


@router.post("/users/{user_id}", dependencies=[Depends(deps.csrf_protect)])
def admin_user_action(
    user_id: str,
    action: str = Form(...),
    user: User = Depends(deps.get_current_staff),
    db: Session = Depends(get_db),
):
    account = db.get(User, user_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    allowed = {"activate", "suspend", "verify_email", "grant_staff", "revoke_staff", "sign_out_all", "delete_account", "restore_account"}
    if action not in allowed:
        return _user_redirect(user_id, message="Unknown account action", error=True)
    if account.id == user.id:
        return _user_redirect(user_id, message="Use account settings to manage your own account", error=True)

    removes_active_staff = (action in {"suspend", "delete_account"} and account.is_staff and account.is_active) or (action == "revoke_staff" and account.is_staff)
    if removes_active_staff:
        active_staff = db.scalar(select(func.count()).select_from(User).where(User.is_staff.is_(True), User.is_active.is_(True))) or 0
        if active_staff <= 1:
            return _user_redirect(user_id, message="The last active staff account cannot be changed", error=True)

    labels = {
        "activate": "Account activated",
        "suspend": "Account suspended and future sign-ins blocked",
        "verify_email": "Email marked verified",
        "grant_staff": "Staff access granted",
        "revoke_staff": "Staff access revoked",
        "sign_out_all": "All active sessions were signed out",
        "delete_account": "Account moved to recoverable deletion and signed out everywhere",
        "restore_account": "Account restored",
    }
    if action == "activate":
        if account.deleted_at is not None:
            return _user_redirect(user_id, message="Restore this deleted account instead", error=True)
        account.is_active = True
        account.state = "active"
    elif action == "suspend":
        account.is_active = False
        account.state = "suspended"
    elif action == "verify_email":
        account.email_verified_at = utcnow()
    elif action == "grant_staff":
        account.is_staff = True
    elif action == "revoke_staff":
        account.is_staff = False
    elif action == "sign_out_all":
        auth_service.revoke_all_sessions(db, account)
    elif action == "delete_account":
        account.is_active = False
        account.state = "deleted"
        account.deleted_at = utcnow()
        account.deleted_by_id = user.id
        auth_service.revoke_all_sessions(db, account)
    elif action == "restore_account":
        account.is_active = True
        account.state = "active"
        account.deleted_at = None
        account.deleted_by_id = None

    db.flush()
    audit_service.record(db, actor=user, action=action, subject_type="user", subject_id=account.id)
    log.info("staff_account_action actor_id=%s target_id=%s action=%s", user.id, account.id, action)
    return _user_redirect(user_id, message=labels[action])


@router.get("/users/{user_id}/purge/reauth")
def admin_purge_reauth(user_id: str, user: User = Depends(deps.get_current_staff), db: Session = Depends(get_db)):
    account = db.get(User, user_id)
    if account is None or account.deleted_at is None or account.id == user.id:
        raise HTTPException(status_code=404, detail="Deleted account not found")
    if not user.tg11_user_id:
        return _user_redirect(user_id, message="Link your TG11 account before permanently deleting an account", error=True)
    try:
        from . import auth as auth_router

        return auth_router.begin_tg11_reauth(target_id=account.id, next_url=f"/admin/users/{account.id}")
    except Exception:
        log.exception("could not start TG11 reauthentication actor_id=%s", user.id)
        return _user_redirect(user_id, message="TG11 reauthentication is unavailable", error=True)


@router.post("/users/{user_id}/purge", dependencies=[Depends(deps.csrf_protect)])
def admin_purge_account(user_id: str, request: Request, confirm: str = Form(""), user: User = Depends(deps.get_current_staff), db: Session = Depends(get_db)):
    account = db.get(User, user_id)
    if account is None or account.deleted_at is None or account.id == user.id:
        raise HTTPException(status_code=404, detail="Deleted account not found")
    if confirm.strip() != account.email:
        return _user_redirect(user_id, message="Type the account email to confirm permanent deletion", error=True)
    if not _has_fresh_purge_reauth(request, db, user, account.id):
        return _user_redirect(user_id, message="A fresh TG11 reauthentication is required", error=True)
    audit_service.record(db, actor=user, action="permanently_delete_account", subject_type="user", subject_id=account.id, detail=account.email)
    db.delete(account)
    db.flush()
    log.warning("staff_permanent_account_delete actor_id=%s target_id=%s", user.id, user_id)
    return deps.redirect("/admin?msg=Account+permanently+deleted")


@router.post("/boards/{board_id}", dependencies=[Depends(deps.csrf_protect)])
def admin_board_action(board_id: str, action: str = Form(...), user: User = Depends(deps.get_current_staff), db: Session = Depends(get_db)):
    board = db.get(Board, board_id)
    if board is None or action != "restore" or board.deleted_at is None:
        raise HTTPException(status_code=404, detail="Recoverable board not found")
    board.deleted_at = None
    board.deleted_by_id = None
    board.is_archived = False
    db.flush()
    audit_service.record(db, actor=user, action="restore_board", subject_type="board", subject_id=board.id, detail=board.name)
    return _user_redirect(board.owner_id, message=f"Board restored: {board.name}")


@router.post("/credentials/{credential_id}", dependencies=[Depends(deps.csrf_protect)])
def admin_credential_action(credential_id: str, action: str = Form(...), user: User = Depends(deps.get_current_staff), db: Session = Depends(get_db)):
    credential = db.get(AIProviderCredential, credential_id)
    if credential is None or action != "restore" or credential.deleted_at is None:
        raise HTTPException(status_code=404, detail="Recoverable credential not found")
    credential.deleted_at = None
    credential.deleted_by_id = None
    credential.enabled = True
    db.flush()
    audit_service.record(db, actor=user, action="restore_credential", subject_type="credential", subject_id=credential.id, detail=credential.provider)
    return _user_redirect(credential.user_id, message=f"{credential.provider} credential restored")


@router.get("/network")
def admin_network(request: Request, user: User = Depends(deps.get_current_staff), db: Session = Depends(get_db)):
    blocks = list(db.scalars(select(IPBlock).order_by(IPBlock.is_active.desc(), IPBlock.created_at.desc())))
    return deps.render(request, "admin/network.html", {"blocks": blocks, "section": "admin"}, db=db)


@router.post("/network/blocks", dependencies=[Depends(deps.csrf_protect)])
def admin_network_block(request: Request, cidr: str = Form(...), reason: str = Form(""), user: User = Depends(deps.get_current_staff), db: Session = Depends(get_db)):
    try:
        normalized = ip_blocks.normalize_cidr(cidr)
    except ValueError as exc:
        return _network_redirect(message=str(exc), error=True)
    actor_ip = request.client.host if request.client else ""
    actor_address = ip_blocks.client_ip(actor_ip)
    if actor_address is not None and actor_address in ipaddress.ip_network(normalized):
        return _network_redirect(message="That range includes your current address", error=True)
    if db.scalar(select(IPBlock).where(IPBlock.cidr == normalized)) is not None:
        return _network_redirect(message="That address or range is already recorded", error=True)
    block = IPBlock(cidr=normalized, reason=reason.strip()[:300], created_by_id=user.id)
    db.add(block)
    db.flush()
    audit_service.record(db, actor=user, action="block_ip", subject_type="ip_block", subject_id=block.id, detail=normalized)
    return _network_redirect(message=f"Blocked {normalized}")


@router.post("/network/blocks/{block_id}/revoke", dependencies=[Depends(deps.csrf_protect)])
def admin_network_unblock(block_id: str, user: User = Depends(deps.get_current_staff), db: Session = Depends(get_db)):
    block = db.get(IPBlock, block_id)
    if block is None or not block.is_active:
        raise HTTPException(status_code=404, detail="Active block not found")
    block.is_active = False
    block.revoked_at = utcnow()
    block.revoked_by_id = user.id
    db.flush()
    audit_service.record(db, actor=user, action="unblock_ip", subject_type="ip_block", subject_id=block.id, detail=block.cidr)
    return _network_redirect(message=f"Unblocked {block.cidr}")
