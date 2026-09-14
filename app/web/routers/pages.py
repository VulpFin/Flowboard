# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""The pages a service owes its users: what it is, what it promises, what it
keeps, how it behaves and whether it is up.

They are deliberately plain: rendered templates with no database work except
the one live check on /status, so they answer even when the rest of the
application is having a bad day.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from ... import __version__
from ...db import get_db
from ...models import User
from .. import deps
from ..mdlite import md_lite

router = APIRouter(tags=["pages"])

# One place to bump when the policies change; every policy page shows it.
POLICY = {"effective": "2026-09-14", "updated": "2026-09-14", "version": "1.0"}

CHANGELOG = Path(__file__).resolve().parents[3] / "CHANGELOG.md"


def _page(request: Request, name: str, extra: Optional[Dict[str, Any]] = None):
    return deps.render(request, f"pages/{name}.html", {"policy": POLICY, **(extra or {})})


@router.get("/about")
def about(request: Request):
    return _page(request, "about")


@router.get("/terms")
def terms(request: Request):
    return _page(request, "terms")


@router.get("/privacy")
def privacy(request: Request):
    return _page(request, "privacy")


@router.get("/guidelines")
def guidelines(request: Request):
    return _page(request, "guidelines")


@router.get("/faq")
def faq(request: Request):
    return _page(request, "faq")


@router.get("/changelog")
def changelog(request: Request):
    try:
        body = md_lite(CHANGELOG.read_text(encoding="utf-8"))
    except OSError:
        body = ""
    return _page(request, "changelog", {"body": body})


@router.get("/status")
def status(request: Request, db: Session = Depends(get_db), user: Optional[User] = Depends(deps.get_current_user_optional)):
    """A summary, not a diagnostic: whether the database answers and whether the
    scheduled work has run lately. Detail that would help someone attack the
    service stays off the page."""
    checks = {}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False
    overall = "ok" if all(checks.values()) else "degraded"
    return _page(request, "status", {
        "overall": overall,
        "checks": checks,
        "version": __version__,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })
