# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Operational CLI.

    python -m app.cli create-user  --email ... --username ... [--password ...] [--staff]
    python -m app.cli import-legacy --file flowboard_tasks.json --user <email> [--board "Personal"]
    python -m app.cli rotate-credential-keys
    python -m app.cli gen-keys
    python -m app.cli check

`import-legacy` migrates the pre-2.0 JSON task store into a board owned by
the given user.  It is idempotent: tasks already imported (matched by
legacy_id within that board) are skipped, and the JSON file is never modified.
"""
from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
from typing import Dict, List

from .config import settings


def cmd_gen_keys(_args) -> int:
    from .security.crypto import generate_key

    print("FLOWBOARD_SECRET_KEY=" + secrets.token_urlsafe(48))
    print("FLOWBOARD_CREDENTIAL_ENCRYPTION_KEY=" + generate_key())
    return 0


def cmd_check(_args) -> int:
    from sqlalchemy import text
    from .db import engine
    from .security.crypto import get_cipher

    print(f"env={settings.FLOWBOARD_ENV} site={settings.FLOWBOARD_SITE_URL}")
    print(f"database={settings.database_url}")
    with engine.connect() as c:
        rev = c.execute(text("select version_num from alembic_version")).scalar() if engine.dialect.has_table(c, "alembic_version") else None
    print(f"alembic revision={rev}")
    try:
        get_cipher()
        print("credential cipher: OK")
    except Exception as exc:
        print(f"credential cipher: NOT CONFIGURED ({exc})")
        return 1
    print(f"oidc configured={settings.oidc_configured} google={settings.google_configured} microsoft={settings.microsoft_configured}")
    return 0


def cmd_create_user(args) -> int:
    from .db import session_scope
    from .services import auth

    password = args.password or getpass.getpass("Password: ")
    with session_scope() as db:
        user = auth.create_user(db, email=args.email, username=args.username, password=password, display_name=args.display_name or args.username, email_verified=True, is_staff=bool(args.staff))
        print(f"created user {user.username} <{user.email}> id={user.id}")
    return 0


def cmd_import_legacy(args) -> int:
    from .db import session_scope
    from .models import Task
    from .services import auth, boards, tasks as task_service
    from sqlalchemy import select

    with open(args.file, "r", encoding="utf-8") as f:
        raw: Dict[str, dict] = json.load(f)
    if not isinstance(raw, dict):
        print("unexpected JSON shape (expected {id: task})", file=sys.stderr)
        return 2
    with session_scope() as db:
        user = auth.get_user_by_email(db, args.user) or auth.get_user_by_username(db, args.user)
        if user is None:
            print(f"user {args.user} not found - create it first (create-user)", file=sys.stderr)
            return 2
        auth.ensure_profile(db, user)
        board = None
        if args.board:
            for b in boards.list_boards_for_user(db, user, include_archived=True):
                if b.name.lower() == args.board.lower() or b.slug == args.board:
                    board = b
                    break
            if board is None:
                board = boards.create_board(db, user, name=args.board, description="Imported from Flowboard 1.x")
        else:
            board = auth.ensure_default_board(db, user)
        existing = {t.legacy_id for t in db.scalars(select(Task).where(Task.board_id == board.id, Task.legacy_id.is_not(None)))}
        id_map: Dict[str, str] = {t.legacy_id: t.id for t in db.scalars(select(Task).where(Task.board_id == board.id, Task.legacy_id.is_not(None)))}
        imported, skipped = 0, 0
        pending_deps: List[tuple] = []
        for legacy_id, item in raw.items():
            if legacy_id in existing:
                skipped += 1
                continue
            t = task_service.create_task(
                db, board,
                {
                    "title": item.get("title") or "(untitled)",
                    "description": item.get("description", ""),
                    "estimate_min": item.get("estimate_min", 30),
                    "importance": item.get("importance", 3),
                    "energy": item.get("energy", "medium"),
                    "due": item.get("due"),
                    "contexts": item.get("contexts") or ["General"],
                    "manual_order": item.get("manual_order", 0),
                },
                actor_id=user.id, actor_kind="system",
            )
            t.legacy_id = legacy_id
            t.last_actual_min = item.get("last_actual_min")
            t.completions = int(item.get("completions") or 0)
            if item.get("done"):
                t.done, t.status = True, "done"
            created = item.get("created")
            if created:
                try:
                    from datetime import datetime

                    t.created_at = datetime.strptime(created, "%Y-%m-%d %H:%M")
                except ValueError:
                    pass
            id_map[legacy_id] = t.id
            pending_deps.append((t, item.get("depends_on") or []))
            imported += 1
        for t, deps in pending_deps:
            t.depends_on = [id_map[d] for d in deps if d in id_map]
        db.flush()
        print(f"imported {imported} task(s) into board '{board.name}' ({board.slug}) for {user.username}; skipped {skipped} already-imported")
    return 0


def cmd_rotate(_args) -> int:
    from sqlalchemy import select
    from .db import session_scope
    from .models import AIProviderCredential, CalendarConnection
    from .security.crypto import get_cipher
    from .ai import credentials as cred_service

    cipher = get_cipher()
    n = 0
    with session_scope() as db:
        for cred in db.scalars(select(AIProviderCredential)):
            if cred_service.rotate_if_needed(db, cred):
                n += 1
        for conn in db.scalars(select(CalendarConnection)):
            if cipher.needs_rotation(conn.token_blob):
                aad = f"calendar-token:{conn.user_id}:{conn.provider}"
                data = cipher.decrypt_json(conn.token_blob, aad)
                conn.token_blob, conn.key_version = cipher.encrypt_json(data, aad)
                n += 1
    print(f"re-encrypted {n} secret(s) with key version {cipher.current_version}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="flowboard")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-keys").set_defaults(fn=cmd_gen_keys)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    cu = sub.add_parser("create-user")
    cu.add_argument("--email", required=True)
    cu.add_argument("--username", required=True)
    cu.add_argument("--password")
    cu.add_argument("--display-name", dest="display_name")
    cu.add_argument("--staff", action="store_true")
    cu.set_defaults(fn=cmd_create_user)
    il = sub.add_parser("import-legacy")
    il.add_argument("--file", required=True)
    il.add_argument("--user", required=True, help="email or username of the owner")
    il.add_argument("--board", help="board name/slug (default: the user's default board)")
    il.set_defaults(fn=cmd_import_legacy)
    sub.add_parser("rotate-credential-keys").set_defaults(fn=cmd_rotate)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
