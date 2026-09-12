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
#: separates the owner from the slug in a *qualified* board reference,
#: "alice~personal".  `slugify` can never produce it, so it is unambiguous.
REF_SEP = "~"


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


def ref_for(board: Board, user: Optional[User]) -> str:
    """The URL reference this user should use for this board.

    Slugs are unique per *owner*, so on a shared board two people can each have
    a board called "personal".  Your own boards keep the bare slug; a board
    somebody else owns is addressed as "<owner>~<slug>", which is unique and
    stable even if you later create a board with the same name.
    """
    if user is not None and board.owner_id == user.id:
        return board.slug
    owner = board.owner
    return f"{owner.username}{REF_SEP}{board.slug}" if owner is not None else board.id


def parse_ref(ref: str) -> tuple:
    """"alice~personal" -> ("alice", "personal"); "personal" -> (None, "personal")."""
    ref = (ref or "").strip()
    if REF_SEP in ref:
        owner, _, slug = ref.partition(REF_SEP)
        return owner.strip().lower() or None, slug.strip()
    return None, ref


def get_board_for_user(db: Session, user: User, slug_or_id: str, *, require: BoardRole = BoardRole.VIEWER) -> Board:
    """Resolve a board by reference (slug, "owner~slug" or UUID) and enforce the
    required role.  Never leaks existence of other users' boards: both "not
    found" and "not yours" raise BoardNotFound.

    Resolution order, so that a slug you share with someone else is never
    ambiguous: UUID, then the qualified "owner~slug" form, then a board *you
    own* with that slug, and only then a board you are a member of.
    """
    board: Optional[Board] = None
    owner_name, slug = parse_ref(slug_or_id)
    if slug_or_id and len(slug_or_id) == 36 and slug_or_id.count("-") == 4:
        board = db.get(Board, slug_or_id)
    elif owner_name:
        # a qualified reference names exactly one board: never fall back to a
        # board of your own that happens to share the slug
        owner = db.scalar(select(User).where(User.username == owner_name))
        if owner is not None:
            board = db.scalar(select(Board).where(Board.owner_id == owner.id, Board.slug == slug))
        if board is None:
            raise BoardNotFound(slug_or_id)
    if board is None:
        # your own board always wins over a shared board with the same slug
        board = db.scalar(select(Board).where(Board.owner_id == user.id, Board.slug == slug))
    if board is None:
        # …otherwise the oldest board you were given with that slug (the
        # qualified form above addresses any of them explicitly)
        board = db.scalar(
            select(Board)
            .join(BoardMembership, BoardMembership.board_id == Board.id)
            .where(Board.slug == slug, BoardMembership.user_id == user.id, BoardMembership.accepted.is_(True))
            .order_by(BoardMembership.created_at, Board.created_at)
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


def member_list(db: Session, board: Board) -> List[Dict]:
    """[{id, name, email, role, user, membership}] for the board, owner first.
    Used by the assignee picker, the members panel and the "who has room this
    week" panel."""
    rank = {BoardRole.OWNER.value: 0, BoardRole.ADMIN.value: 1, BoardRole.EDITOR.value: 2, BoardRole.VIEWER.value: 3}
    out: List[Dict] = []
    for m in sorted(board.memberships, key=lambda m: (rank.get(m.role, 9), m.created_at or "")):
        if not m.accepted:
            continue
        u = db.get(User, m.user_id)
        if u is None:
            continue
        out.append({"id": u.id, "name": u.display_name or u.username, "email": u.email, "role": m.role,
                    "user": u, "membership": m, "is_owner": u.id == board.owner_id})
    return out


# --- membership management -------------------------------------------------

def add_member(db: Session, board: Board, user: User, role: BoardRole = BoardRole.EDITOR, *, invited_by_id: Optional[str] = None) -> BoardMembership:
    """Idempotent: an existing membership is returned (its role upgraded if the
    new one is higher), never duplicated."""
    existing = db.scalar(select(BoardMembership).where(BoardMembership.board_id == board.id, BoardMembership.user_id == user.id))
    rank = {BoardRole.VIEWER: 0, BoardRole.EDITOR: 1, BoardRole.ADMIN: 2, BoardRole.OWNER: 3}
    if existing is not None:
        if rank[BoardRole(existing.role)] < rank[role] and existing.role != BoardRole.OWNER.value:
            existing.role = role.value
        existing.accepted = True
        db.flush()
        return existing
    m = BoardMembership(board_id=board.id, user_id=user.id, role=role.value, accepted=True, invited_by_id=invited_by_id)
    db.add(m)
    db.flush()
    return m


def set_member_role(db: Session, board: Board, member_user_id: str, role: BoardRole) -> BoardMembership:
    if member_user_id == board.owner_id:
        raise ValueError("The board owner's role cannot be changed")
    if role == BoardRole.OWNER:
        raise ValueError("Ownership is not transferable here")
    m = db.scalar(select(BoardMembership).where(BoardMembership.board_id == board.id, BoardMembership.user_id == member_user_id))
    if m is None:
        raise ValueError("Not a member of this board")
    m.role = role.value
    db.flush()
    return m


def remove_member(db: Session, board: Board, member_user_id: str) -> None:
    """Remove someone from a board: their memberships go, tasks assigned to them
    on this board fall back to the owner's capacity, and the board stops being
    their default."""
    from ..models import Task, UserProfile

    if member_user_id == board.owner_id:
        raise ValueError("The owner cannot be removed from their own board")
    m = db.scalar(select(BoardMembership).where(BoardMembership.board_id == board.id, BoardMembership.user_id == member_user_id))
    if m is None:
        raise ValueError("Not a member of this board")
    for t in db.scalars(select(Task).where(Task.board_id == board.id, Task.assigned_to_id == member_user_id)):
        t.assigned_to_id = None
        t.scheduled_date, t.scheduled_start = None, None  # re-plan against the owner instead
    profile = db.scalar(select(UserProfile).where(UserProfile.user_id == member_user_id))
    if profile is not None and profile.default_board_id == board.id:
        profile.default_board_id = None
    db.delete(m)
    db.flush()


def member_names(db: Session, board: Board) -> Dict[str, str]:
    return {m["id"]: m["name"] for m in member_list(db, board)}


def board_settings(board: Board) -> Dict:
    try:
        return json.loads(board.settings_json or "{}")
    except Exception:
        return {}


def delete_board(db: Session, board: Board) -> None:
    db.delete(board)
    db.flush()
