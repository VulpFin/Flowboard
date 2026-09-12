# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Connections (OAuth accounts) and task<->event links.  One-way push:
Flowboard -> external calendar.  Tokens are encrypted at rest with the same
AES-GCM envelope as AI credentials (AAD binds them to the owning user)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Board, CalendarConnection, CalendarEventLink, Task, User, utcnow
from ..security.crypto import get_cipher
from ..services.tasks import parse_due
from .ics import content_fingerprint
from .providers import CalendarProvider, CalendarProviderError, get_provider


def _aad(user_id: str, provider: str) -> str:
    return f"calendar-token:{user_id}:{provider}"


# --- connections --------------------------------------------------------

def list_connections(db: Session, user: User) -> List[CalendarConnection]:
    return list(db.scalars(select(CalendarConnection).where(CalendarConnection.user_id == user.id).order_by(CalendarConnection.created_at)))


def get_connection(db: Session, user: User, conn_id: str) -> Optional[CalendarConnection]:
    c = db.get(CalendarConnection, conn_id)
    if c is None or c.user_id != user.id:
        return None
    return c


def store_connection(db: Session, user: User, provider_id: str, token: Dict[str, Any], account: Dict[str, str], scopes: str) -> CalendarConnection:
    cipher = get_cipher()
    blob, ver = cipher.encrypt_json(token, _aad(user.id, provider_id))
    existing = db.scalar(select(CalendarConnection).where(CalendarConnection.user_id == user.id, CalendarConnection.provider == provider_id, CalendarConnection.external_account_id == (account.get("id") or "")))
    conn = existing or CalendarConnection(user_id=user.id, provider=provider_id)
    conn.token_blob, conn.key_version = blob, ver
    conn.account_email = account.get("email", "")[:254]
    conn.external_account_id = (account.get("id") or "")[:255]
    conn.scopes = scopes[:500]
    conn.status, conn.status_message, conn.enabled = "connected", "", True
    db.add(conn)
    db.flush()
    return conn


def _token(conn: CalendarConnection) -> Dict[str, Any]:
    return get_cipher().decrypt_json(conn.token_blob, _aad(conn.user_id, conn.provider))


def _save_token(db: Session, conn: CalendarConnection, token: Dict[str, Any]) -> None:
    conn.token_blob, conn.key_version = get_cipher().encrypt_json(token, _aad(conn.user_id, conn.provider))
    db.flush()


def fresh_token(db: Session, conn: CalendarConnection, provider: Optional[CalendarProvider] = None) -> Dict[str, Any]:
    provider = provider or get_provider(conn.provider)
    token = _token(conn)
    if provider.token_expired(token):
        try:
            token = provider.refresh(token)
        except CalendarProviderError as exc:
            conn.status = "error"
            conn.status_message = str(exc)[:300]
            db.flush()
            raise
        _save_token(db, conn, token)
    conn.last_used_at = utcnow()
    return token


def refresh_calendar_list(db: Session, conn: CalendarConnection) -> List[Dict[str, Any]]:
    provider = get_provider(conn.provider)
    cals = provider.list_calendars(fresh_token(db, conn, provider))
    conn.calendars_cache_json = json.dumps(cals)
    if not conn.target_calendar_id:
        primary = next((c for c in cals if c.get("primary")), cals[0] if cals else None)
        if primary:
            conn.target_calendar_id, conn.target_calendar_name = primary["id"], primary["name"]
    db.flush()
    return cals


def cached_calendars(conn: CalendarConnection) -> List[Dict[str, Any]]:
    try:
        return json.loads(conn.calendars_cache_json or "[]")
    except Exception:
        return []


def set_target_calendar(db: Session, conn: CalendarConnection, calendar_id: str) -> None:
    for c in cached_calendars(conn):
        if c["id"] == calendar_id:
            conn.target_calendar_id, conn.target_calendar_name = c["id"], c["name"]
            db.flush()
            return
    raise ValueError("unknown calendar")


def disconnect(db: Session, conn: CalendarConnection) -> None:
    """Remove the connection and its links.  Events already created remain in
    the external calendar (we do not delete user data on disconnect)."""
    for link in db.scalars(select(CalendarEventLink).where(CalendarEventLink.connection_id == conn.id)):
        db.delete(link)
    db.delete(conn)
    db.flush()


# --- links ----------------------------------------------------------------

def event_payload(task: Task, board: Board, *, timezone: str = "UTC", default_duration_min: int = 60) -> Dict[str, Any]:
    due = task.due_at or parse_due(task.due)
    if due is None:
        raise ValueError("Task has no due date - set one before adding it to a calendar")
    all_day = bool(task.due and len(task.due) <= 10)
    if all_day:
        start: Any = due.date()
        end: Any = due.date() + timedelta(days=1)
    else:
        duration = max(5, task.estimate_min or default_duration_min)
        start, end = due - timedelta(minutes=duration), due
    desc = (task.description or "") + f"\n\nFlowboard · {board.name} · est {task.estimate_min} min · importance {task.importance}/5"
    return {"summary": f"[{task.primary_context}] {task.title}", "description": desc.strip(), "start": start, "end": end, "all_day": all_day, "timezone": timezone, "task_id": task.id}


def links_for_task(db: Session, task: Task) -> List[CalendarEventLink]:
    return list(db.scalars(select(CalendarEventLink).where(CalendarEventLink.task_id == task.id)))


def links_for_board(db: Session, board: Board) -> Dict[str, List[CalendarEventLink]]:
    out: Dict[str, List[CalendarEventLink]] = {}
    for l in db.scalars(select(CalendarEventLink).where(CalendarEventLink.board_id == board.id)):
        out.setdefault(l.task_id, []).append(l)
    return out


def push_task(db: Session, user: User, board: Board, task: Task, conn: CalendarConnection, *, timezone: str = "UTC") -> CalendarEventLink:
    """Create (or update if already linked) the external event for a task."""
    assert conn.user_id == user.id and task.board_id == board.id
    if not conn.target_calendar_id:
        refresh_calendar_list(db, conn)
    provider = get_provider(conn.provider)
    payload = event_payload(task, board, timezone=timezone)
    link = db.scalar(select(CalendarEventLink).where(CalendarEventLink.task_id == task.id, CalendarEventLink.connection_id == conn.id))
    token = fresh_token(db, conn, provider)
    try:
        if link is None:
            created = provider.create_event(token, conn.target_calendar_id, payload)
            link = CalendarEventLink(task_id=task.id, board_id=board.id, user_id=user.id, connection_id=conn.id, provider=conn.provider, external_calendar_id=conn.target_calendar_id, external_event_id=created["id"], external_url=created.get("url", "")[:500])
            db.add(link)
        else:
            provider.update_event(token, link.external_calendar_id, link.external_event_id, payload)
        link.sync_status, link.last_error, link.last_synced_at = "synced", "", utcnow()
        link.fingerprint = content_fingerprint(task)
    except CalendarProviderError as exc:
        if link is None:
            raise
        link.sync_status, link.last_error = "error", str(exc)[:500]
    db.flush()
    return link


def sync_task_links(db: Session, task: Task, board: Board, *, timezone: str = "UTC") -> List[CalendarEventLink]:
    """One-way update after a task changed (called from the task routes).
    Failures are recorded on the link, never raised into the UI."""
    out = []
    for link in links_for_task(db, task):
        if not link.auto_update:
            continue
        if link.fingerprint == content_fingerprint(task):
            continue
        conn = db.get(CalendarConnection, link.connection_id)
        if conn is None or not conn.enabled:
            continue
        try:
            provider = get_provider(conn.provider)
            token = fresh_token(db, conn, provider)
            if task.due_at is None:
                provider.delete_event(token, link.external_calendar_id, link.external_event_id)
                link.sync_status, link.last_error = "orphaned", "task lost its due date; event removed"
            else:
                provider.update_event(token, link.external_calendar_id, link.external_event_id, event_payload(task, board, timezone=timezone))
                link.sync_status, link.last_error, link.last_synced_at = "synced", "", utcnow()
                link.fingerprint = content_fingerprint(task)
        except (CalendarProviderError, ValueError) as exc:
            link.sync_status, link.last_error = "error", str(exc)[:500]
        out.append(link)
    db.flush()
    return out


def unlink(db: Session, user: User, link: CalendarEventLink, *, delete_remote: bool = False) -> None:
    assert link.user_id == user.id
    if delete_remote:
        conn = db.get(CalendarConnection, link.connection_id)
        if conn is not None:
            try:
                provider = get_provider(conn.provider)
                provider.delete_event(fresh_token(db, conn, provider), link.external_calendar_id, link.external_event_id)
            except CalendarProviderError:
                pass
    db.delete(link)
    db.flush()
