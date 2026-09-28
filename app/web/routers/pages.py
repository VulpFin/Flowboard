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
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from ... import __version__
from ...config import settings
from ...db import get_db
from ...ai.registry import list_specs
from ...calendars.providers import CALENDAR_PROVIDERS, GOOGLE_CALENDAR_SCOPES
from ...models import User
from ...services import auth as auth_service
from ...services import boards as board_service
from ...services import tasks as task_service
from ...calendars import links as calendar_links
from .. import deps
from ..mdlite import md_lite

router = APIRouter(tags=["pages"])

# One place to bump when the policies change; every policy page shows it.
POLICY = {"effective": "2026-09-27", "updated": "2026-09-27", "version": "1.2"}

CHANGELOG = Path(__file__).resolve().parents[3] / "CHANGELOG.md"

SEO_PAGES = {
    "home": "Plan work around real time with boards, schedules, calendar-aware capacity, and the services you choose to connect.",
    "about": "Learn about Vulpfin Flowboard, a planning workspace for personal and shared work.",
    "faq": "Answers to common questions about Flowboard planning, boards, calendars, privacy, and connected services.",
    "guidelines": "Flowboard community and product-use guidelines.",
    "third_party_services": "How Flowboard's optional calendar and AI provider integrations handle your data.",
    "privacy": "Flowboard privacy policy and data-handling commitments.",
    "terms": "Flowboard terms of service.",
    "changelog": "Flowboard release notes and product changes.",
}


def _page(request: Request, name: str, extra: Optional[Dict[str, Any]] = None):
    return deps.render(request, f"pages/{name}.html", {
        "policy": POLICY,
        "seo_indexable": name in SEO_PAGES,
        "meta_description": SEO_PAGES.get(name, ""),
        **(extra or {}),
    })


@router.get("/robots.txt", include_in_schema=False)
def robots_txt():
    body = "\n".join((
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin/",
        "Disallow: /boards/",
        "Disallow: /calendar/",
        "Disallow: /settings/",
        "Disallow: /auth/",
        "Disallow: /login",
        "Disallow: /register",
        "Disallow: /support",
        f"Sitemap: {settings.absolute_url('/sitemap.xml')}",
        "",
    ))
    return PlainTextResponse(body)


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml():
    urls = "".join(
        f"<url><loc>{settings.absolute_url('/' if name == 'home' else '/' + name)}</loc></url>"
        for name in SEO_PAGES
    )
    body = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(body, media_type="application/xml")


def _integration_context() -> Dict[str, Any]:
    calendars = []
    for provider_id, provider_cls in CALENDAR_PROVIDERS.items():
        provider = provider_cls()
        calendars.append({
            "id": provider_id,
            "name": provider.name,
            "scopes": provider.scopes.split(),
            "configured": provider.configured,
        })
    return {
        "calendar_providers": calendars,
        "google_scopes": GOOGLE_CALENDAR_SCOPES,
        "ai_providers": [
            {"id": spec.id, "name": spec.name, "status": spec.status}
            for spec in list_specs()
            if spec.status in {"supported", "beta"}
        ],
    }


@router.get("/")
def home(request: Request, user: Optional[User] = Depends(deps.get_current_user_optional), db: Session = Depends(get_db)):
    """Public product page for visitors; practical landing page for members."""
    if user is None:
        return _page(request, "home", _integration_context())
    default_board = auth_service.ensure_default_board(db, user)
    boards = board_service.list_boards_for_user(db, user)
    counts = {
        board.id: task_service.board_stats(task_service.list_tasks(db, board, include_done=True))
        for board in boards
    }
    return deps.render(request, "pages/dashboard.html", {
        "default_board": default_board,
        "recent_boards": boards[:6],
        "counts": counts,
        "connections": calendar_links.list_connections(db, user),
    }, db=db)


@router.get("/about")
def about(request: Request):
    return _page(request, "about")


@router.get("/terms")
def terms(request: Request):
    return _page(request, "terms")


@router.get("/privacy")
def privacy(request: Request):
    return _page(request, "privacy")


@router.get("/third-party-services")
def third_party_services(request: Request):
    return _page(request, "third_party_services", _integration_context())


@router.get("/oauth-review")
def oauth_review(request: Request):
    return _page(request, "oauth_review", _integration_context())


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
