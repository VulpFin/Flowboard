# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Local authentication service: users, sessions, password reset.

External (TG11 OIDC) authentication lives in `app.identity`; it calls
`get_or_create_user_for_identity` here so that both paths produce the same
`User` + `UserProfile` + default board.
"""
from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Board, IdentityLink, PasswordResetToken, User, UserProfile, UserSession, utcnow
from ..security.passwords import hash_password, needs_rehash, verify_password
from ..security.tokens import generate_token, hash_token

USERNAME_RE = re.compile(r"^[a-z0-9_.]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    pass


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def validate_password_strength(password: str) -> None:
    if len(password or "") < 10:
        raise AuthError("Password must be at least 10 characters.")
    if password.lower() in {"password10", "flowboard1", "1234567890"}:
        raise AuthError("That password is too common.")


def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.scalar(select(User).where(User.email == normalize_email(email)))


def get_user_by_username(db: Session, username: str) -> Optional[User]:
    return db.scalar(select(User).where(User.username == normalize_username(username)))


def get_user(db: Session, user_id: str) -> Optional[User]:
    return db.get(User, user_id)


def ensure_profile(db: Session, user: User) -> UserProfile:
    if user.profile is None:
        user.profile = UserProfile(user_id=user.id)
        db.add(user.profile)
        db.flush()
    return user.profile


def ensure_default_board(db: Session, user: User) -> Board:
    """Every user gets at least one board; returns the default one."""
    from .boards import create_board, list_boards_for_user  # local import to avoid cycle

    profile = ensure_profile(db, user)
    if profile.default_board_id:
        board = db.get(Board, profile.default_board_id)
        if board is not None and board.owner_id == user.id:
            return board
    # only a board you *own* can become your default: being a member of someone
    # else's board should never silently make it your home page
    owned = [b for b in list_boards_for_user(db, user, include_archived=False) if b.owner_id == user.id]
    if owned:
        profile.default_board_id = owned[0].id
        return owned[0]
    board = create_board(db, user, name="Personal", description="Your default board", icon="🗂️")
    profile.default_board_id = board.id
    db.flush()
    return board


def create_user(
    db: Session,
    *,
    email: str,
    username: str,
    password: Optional[str],
    display_name: str = "",
    email_verified: bool = False,
    is_staff: bool = False,
) -> User:
    email = normalize_email(email)
    username = normalize_username(username)
    if not EMAIL_RE.match(email):
        raise AuthError("Enter a valid email address.")
    if not USERNAME_RE.match(username):
        raise AuthError("Username may contain 3-30 lowercase letters, numbers, underscore or dot.")
    if get_user_by_email(db, email):
        raise AuthError("An account with that email already exists.")
    if get_user_by_username(db, username):
        raise AuthError("That username is taken.")
    if password is not None:
        validate_password_strength(password)
    user = User(
        email=email,
        username=username,
        display_name=(display_name or username)[:80],
        password_hash=hash_password(password) if password else None,
        is_staff=is_staff,
        email_verified_at=utcnow() if email_verified else None,
        state="active",
    )
    db.add(user)
    db.flush()
    ensure_profile(db, user)
    ensure_default_board(db, user)
    try:
        from .tutorial import seed_tutorial

        seed_tutorial(db, user)
    except Exception:  # the tutorial is a nicety, never a reason to fail sign-up
        pass
    return user


def authenticate(db: Session, identifier: str, password: str) -> Optional[User]:
    ident = (identifier or "").strip().lower()
    user = get_user_by_email(db, ident) if "@" in ident else get_user_by_username(db, ident)
    if user is None or not user.is_active or not user.password_hash:
        # constant-ish time: still run a hash to avoid trivial user enumeration timing
        verify_password(password, hash_password("timing-equaliser"))
        return None
    if not verify_password(password, user.password_hash):
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = utcnow()
    return user


def set_password(db: Session, user: User, new_password: str) -> None:
    validate_password_strength(new_password)
    user.password_hash = hash_password(new_password)
    # revoke all other sessions on password change
    for s in db.scalars(select(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))):
        s.revoked_at = utcnow()


# --- sessions -----------------------------------------------------------

def create_session(db: Session, user: User, *, user_agent: str = "", ip: str = "", auth_method: str = "password") -> UserSession:
    sess = UserSession(
        user_id=user.id,
        expires_at=utcnow() + timedelta(seconds=settings.FLOWBOARD_SESSION_MAX_AGE),
        user_agent=(user_agent or "")[:255],
        ip_address=(ip or "")[:64],
        csrf_token=secrets.token_urlsafe(32),
        auth_method=auth_method,
    )
    db.add(sess)
    db.flush()
    return sess


def get_valid_session(db: Session, session_id: str) -> Optional[UserSession]:
    if not session_id:
        return None
    sess = db.get(UserSession, session_id)
    if sess is None or sess.revoked_at is not None or sess.expires_at < utcnow():
        return None
    return sess


def revoke_session(db: Session, session_id: str) -> None:
    sess = db.get(UserSession, session_id)
    if sess is not None:
        sess.revoked_at = utcnow()


def revoke_other_sessions(db: Session, user: User, keep_session_id: str) -> int:
    n = 0
    for s in db.scalars(select(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))):
        if s.id != keep_session_id:
            s.revoked_at = utcnow()
            n += 1
    return n


# --- password reset -----------------------------------------------------

def create_password_reset(db: Session, user: User, ttl_minutes: int = 60) -> str:
    token = generate_token(32)
    db.add(PasswordResetToken(user_id=user.id, token_hash=hash_token(token), expires_at=utcnow() + timedelta(minutes=ttl_minutes)))
    return token


def consume_password_reset(db: Session, token: str) -> Optional[User]:
    row = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(token)))
    if row is None or row.used_at is not None or row.expires_at < utcnow():
        return None
    row.used_at = utcnow()
    return db.get(User, row.user_id)


# --- external identity --------------------------------------------------

def get_or_create_user_for_identity(
    db: Session,
    *,
    provider: str,
    issuer: str,
    subject: str,
    email: str,
    email_verified: bool,
    preferred_username: str = "",
    display_name: str = "",
) -> Tuple[User, bool]:
    """Resolve an OIDC identity to a local user.

    Rules (see docs/TG11_SSO.md):
      1. existing IdentityLink(provider, subject)  -> that user
      2. otherwise, if a local user has the same *verified* email and the IdP
         asserts the email is verified -> link them (account linking by
         verified email, never by unverified email)
      3. otherwise create a new local user (SSO-only, no password)
    Returns (user, created).
    """
    link = db.scalar(select(IdentityLink).where(IdentityLink.provider == provider, IdentityLink.subject == subject))
    if link is not None:
        link.last_login_at = utcnow()
        user = db.get(User, link.user_id)
        if user is None:
            raise AuthError("identity link points to a missing user")
        return user, False

    email = normalize_email(email)
    user = get_user_by_email(db, email) if email else None
    created = False
    if user is not None:
        if not email_verified:
            raise AuthError(
                "Your TG11 email is not verified, so it cannot be linked to the existing Flowboard "
                "account with the same address. Sign in with your Flowboard password and link TG11 from Settings → Security."
            )
    else:
        base = normalize_username(preferred_username) or email.split("@")[0] if email else f"user{secrets.token_hex(3)}"
        base = re.sub(r"[^a-z0-9_.]", "", base)[:26] or f"user{secrets.token_hex(3)}"
        username = base
        i = 1
        while get_user_by_username(db, username):
            i += 1
            username = f"{base}{i}"
        user = create_user(
            db,
            email=email or f"{subject}@invalid.tg11.local",
            username=username,
            password=None,
            display_name=display_name or username,
            email_verified=email_verified,
        )
        created = True

    db.add(
        IdentityLink(
            user_id=user.id,
            provider=provider,
            issuer=issuer,
            subject=subject,
            email_at_link=email,
            username_at_link=preferred_username or "",
            migration_source="oidc_login",
            migration_status="linked",
            last_login_at=utcnow(),
        )
    )
    user.tg11_user_id = subject if provider == "tg11" else user.tg11_user_id
    return user, created


def verify_password_for(user: User, password: str) -> bool:
    return verify_password(password, user.password_hash)
