# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Board service: creation, lookup and *authorization*.

All access to a board goes through `get_board_for_user`; it raises
`BoardAccessDenied` unless the user has a membership with sufficient role.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Board, BoardMembership, BoardRole, User

RESERVED_SLUGS = {"new", "settings", "archive", "api", "static", "login", "logout", "boards"}


class BoardAccessDenied(Exception):
    pass


class BoardNotFound(Exception):
    pass


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value[:60] or "board"


def unique_slug(db: Session, owner: User, base: str, exclude_id: Optional[str] = None) -> str:
    slug = slugify(base)
    if slug in RESERVED_SLUGS:
        slug = f"{slug}-board"
    candidate, i = slug, 1
    while True:
        q = select(Board.id).where(Board.owner_id == owner.id, Board.slug == candidate)
        if exclude_id:
            q = q.where(Board.id != exclude_id)
        if db.scalar(q) is None:
            return candidate
        i += 1
        candidate = f"{slug}-{i}"


def create_board(
    db: Session,
    owner: User,
    *,
    name: str,
    description: str = "",
    icon: str = "",
    color: str = "",
    settings: Optional[Dict] = None,
) -> Board:
    name = (name or "").strip()[:120]
    if not name:
        raise ValueError("Board name is required")
    board = Board(
        owner_id=owner.id,
        name=name,
        slug=unique_slug(db, owner, name),
        description=(description or "").strip(),
        icon=(icon or "").strip()[:32],
        color=(color or "").strip()[:16],
        settings_json=json.dumps(settings or {}),
        position=len(list_boards_for_user(db, owner, include_archived=True)),
    )
    db.add(board)
    db.flush()
    db.add(BoardMembership(board_id=board.id, user_id=owner.id, role=BoardRole.OWNER.value, accepted=True))
    db.flush()
    return board


def list_boards_for_user(db: Session, user: User, include_archived: bool = False) -> List[Board]:
    q = (
        select(Board)
        .join(BoardMembership, BoardMembership.board_id == Board.id)
        .where(BoardMembership.user_id == user.id, BoardMembership.accepted.is_(True))
        .order_by(Board.is_archived, Board.position, Board.created_at)
    )
    boards = list(db.scalars(q).unique())
    if not include_archived:
        boards = [b for b in boards if not b.is_archived]
    return boards


def membership_for(db: Session, board: Board, user: User) -> Optional[BoardMembership]:
    return db.scalar(
        select(BoardMembership).where(
            BoardMembership.board_id == board.id, BoardMembership.user_id == user.id, BoardMembership.accepted.is_(True)
        )
    )


def get_board_for_user(db: Session, user: User, slug_or_id: str, *, require: BoardRole = BoardRole.VIEWER) -> Board:
    """Resolve a board by slug (within the user's boards) or by UUID, and
    enforce the required role.  Never leaks existence of other users' boards:
    both "not found" and "not yours" raise BoardNotFound."""
    board: Optional[Board] = None
    if slug_or_id and len(slug_or_id) == 36 and slug_or_id.count("-") == 4:
        board = db.get(Board, slug_or_id)
    if board is None:
        board = db.scalar(
            select(Board)
            .join(BoardMembership, BoardMembership.board_id == Board.id)
            .where(Board.slug == slug_or_id, BoardMembership.user_id == user.id, BoardMembership.accepted.is_(True))
        )
    if board is None:
        raise BoardNotFound(slug_or_id)
    m = membership_for(db, board, user)
    if m is None:
        raise BoardNotFound(slug_or_id)
    rank = {BoardRole.VIEWER: 0, BoardRole.EDITOR: 1, BoardRole.ADMIN: 2, BoardRole.OWNER: 3}
    if rank[m.role_enum] < rank[require]:
        raise BoardAccessDenied(f"role {m.role} cannot perform this action")
    return board


def update_board(db: Session, board: Board, **fields) -> Board:
    owner = db.get(User, board.owner_id)
    if "name" in fields and fields["name"]:
        board.name = fields["name"].strip()[:120]
        if fields.get("regenerate_slug"):
            board.slug = unique_slug(db, owner, board.name, exclude_id=board.id)
    for key in ("description", "icon", "color"):
        if key in fields and fields[key] is not None:
            setattr(board, key, str(fields[key]).strip()[:500 if key == "description" else 32])
    if "is_archived" in fields:
        board.is_archived = bool(fields["is_archived"])
    if "settings" in fields and isinstance(fields["settings"], dict):
        board.settings_json = json.dumps(fields["settings"])
    db.flush()
    return board


def board_settings(board: Board) -> Dict:
    try:
        return json.loads(board.settings_json or "{}")
    except Exception:
        return {}


def delete_board(db: Session, board: Board) -> None:
    db.delete(board)
    db.flush()
