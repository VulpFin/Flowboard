# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Board invitations.

An invitation is addressed to an *email*, so you can invite someone who has not
signed up yet - they register with that address and the invitation is waiting
for them at ``/invites``.  The emailed link carries a 256-bit token of which
only the SHA-256 hash is stored (same pattern as password resets and calendar
feeds); it expires, is single use, and is bound to the invited address, so a
leaked link cannot be redeemed by somebody else.

Membership rows are created on *acceptance*, so "member" keeps meaning
"someone who said yes" - which is what capacity, assignment and the
"who has room this week" panel all rely on.
"""
from __future__ import annotations

from datetime import timedelta
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Board, BoardInvite, BoardMembership, BoardRole, User, utcnow
from ..security.tokens import generate_token, hash_token
from . import boards as board_service
from . import mail as mail_service

INVITE_TTL_DAYS = 14
INVITABLE_ROLES = (BoardRole.VIEWER, BoardRole.EDITOR, BoardRole.ADMIN)


class InviteError(Exception):
    pass


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()[:254]


def invite_url(token: str) -> str:
    return settings.absolute_url(f"/invites/{token}")


# --- creating --------------------------------------------------------------

def create(db: Session, board: Board, inviter: User, email: str, role: BoardRole = BoardRole.EDITOR, *, message: str = "") -> Tuple[BoardInvite, str]:
    email = normalize_email(email)
    if "@" not in email or "." not in email.split("@")[-1]:
        raise InviteError("That does not look like an email address")
    if role not in INVITABLE_ROLES:
        raise InviteError("Pick viewer, editor or admin")
    existing_user = db.scalar(select(User).where(User.email == email))
    if existing_user is not None:
        if existing_user.id == board.owner_id:
            raise InviteError("That is the board owner")
        member = db.scalar(select(BoardMembership).where(BoardMembership.board_id == board.id, BoardMembership.user_id == existing_user.id))
        if member is not None:
            raise InviteError(f"{existing_user.username} is already a member")
    # one open invitation per address per board: re-inviting rotates the token
    for old in db.scalars(select(BoardInvite).where(BoardInvite.board_id == board.id, BoardInvite.email == email)):
        if old.is_open:
            old.revoked_at = utcnow()
    token = generate_token()
    invite = BoardInvite(
        board_id=board.id, email=email, role=role.value,
        token_hash=hash_token(token), token_prefix=token[:8],
        invited_by_id=inviter.id, message=(message or "").strip()[:500],
        expires_at=utcnow() + timedelta(days=INVITE_TTL_DAYS),
    )
    db.add(invite)
    db.flush()
    return invite, token


def send(invite: BoardInvite, token: str, board: Board, inviter: User) -> bool:
    return mail_service.send_board_invite(invite.email, board=board, inviter=inviter, url=invite_url(token), role=invite.role, message=invite.message)


# --- reading ---------------------------------------------------------------

def for_board(db: Session, board: Board, *, open_only: bool = True) -> List[BoardInvite]:
    rows = list(db.scalars(select(BoardInvite).where(BoardInvite.board_id == board.id).order_by(BoardInvite.created_at.desc())))
    return [r for r in rows if r.is_open] if open_only else rows


def for_user(db: Session, user: User) -> List[BoardInvite]:
    """Open invitations addressed to this user's email (so the link is optional)."""
    rows = db.scalars(select(BoardInvite).where(BoardInvite.email == normalize_email(user.email)).order_by(BoardInvite.created_at.desc()))
    return [r for r in rows if r.is_open]


def pending_count(db: Session, user: Optional[User]) -> int:
    return len(for_user(db, user)) if user is not None else 0


def resolve(db: Session, token: str) -> Optional[BoardInvite]:
    if not token:
        return None
    return db.scalar(select(BoardInvite).where(BoardInvite.token_hash == hash_token(token)))


def get_for_board(db: Session, board: Board, invite_id: str) -> Optional[BoardInvite]:
    inv = db.get(BoardInvite, invite_id)
    return inv if inv is not None and inv.board_id == board.id else None


# --- acting ----------------------------------------------------------------

def accept(db: Session, invite: BoardInvite, user: User) -> BoardMembership:
    """Join the board.  The signed-in user's address must be the invited one -
    an invitation is not a bearer ticket for whoever opens the link."""
    if not invite.is_open:
        raise InviteError(f"That invitation is {invite.state}")
    if normalize_email(user.email) != invite.email:
        raise InviteError(f"That invitation was sent to {invite.email}; you are signed in as {user.email}")
    board = db.get(Board, invite.board_id)
    if board is None:
        raise InviteError("That board no longer exists")
    membership = board_service.add_member(db, board, user, BoardRole(invite.role), invited_by_id=invite.invited_by_id)
    invite.accepted_at, invite.accepted_by_id = utcnow(), user.id
    db.flush()
    return membership


def decline(db: Session, invite: BoardInvite, user: User) -> None:
    if not invite.is_open:
        raise InviteError(f"That invitation is {invite.state}")
    if normalize_email(user.email) != invite.email:
        raise InviteError("That invitation was sent to a different address")
    invite.declined_at = utcnow()
    db.flush()


def revoke(db: Session, invite: BoardInvite) -> None:
    if invite.is_open:
        invite.revoked_at = utcnow()
        db.flush()
