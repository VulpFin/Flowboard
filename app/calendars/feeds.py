# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Subscription feed tokens.  The token is shown to the user once; only its
SHA-256 is stored.  Feed URLs never contain user or board ids."""
from __future__ import annotations

from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Board, CalendarFeedToken, User, utcnow
from ..security.tokens import generate_token, hash_token


def get_feed(db: Session, user: User, board: Board) -> Optional[CalendarFeedToken]:
    return db.scalar(select(CalendarFeedToken).where(CalendarFeedToken.board_id == board.id, CalendarFeedToken.user_id == user.id))


def create_or_rotate_feed(db: Session, user: User, board: Board, *, include_done: bool = False) -> Tuple[CalendarFeedToken, str]:
    """Return (row, plaintext token).  Rotating invalidates the previous URL."""
    token = generate_token(32)
    row = get_feed(db, user, board)
    if row is None:
        row = CalendarFeedToken(board_id=board.id, user_id=user.id, token_hash=hash_token(token), token_prefix=token[:6], include_done=include_done)
        db.add(row)
    else:
        row.token_hash = hash_token(token)
        row.token_prefix = token[:6]
        row.revoked_at = None
        row.include_done = include_done
        row.created_at = utcnow()
    db.flush()
    return row, token


def revoke_feed(db: Session, row: CalendarFeedToken) -> None:
    row.revoked_at = utcnow()
    db.flush()


def resolve_feed_token(db: Session, token: str) -> Optional[CalendarFeedToken]:
    if not token or len(token) < 20:
        return None
    row = db.scalar(select(CalendarFeedToken).where(CalendarFeedToken.token_hash == hash_token(token)))
    if row is None or row.revoked_at is not None:
        return None
    row.last_fetched_at = utcnow()
    return row


def feed_url(token: str) -> str:
    return settings.absolute_url(f"/calendar/feed/{token}.ics")


def webcal_url(token: str) -> str:
    return feed_url(token).replace("https://", "webcal://", 1).replace("http://", "webcal://", 1)
