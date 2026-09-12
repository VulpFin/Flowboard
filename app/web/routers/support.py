# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
""""Contact support": a footer form that emails the operator.  Works signed out,
because the person who cannot sign in is the one who most needs it."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ...db import get_db
from ...models import User
from ...services import support as support_service
from .. import deps

router = APIRouter(tags=["support"])


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.get("/support")
def support_page(request: Request, from_page: Optional[str] = None, user: Optional[User] = Depends(deps.get_current_user_optional), db: Session = Depends(get_db)):
    return deps.render(request, "support.html", {
        "categories": support_service.CATEGORIES,
        "from_page": (from_page or request.headers.get("referer", ""))[:300],
        "support_email": support_service.settings.support_email,
        "max_message": support_service.MAX_MESSAGE,
    }, db=db)


@router.post("/support", dependencies=[Depends(deps.csrf_protect)])
def support_send(request: Request, email: str = Form(""), category: str = Form("other"), message: str = Form(""), from_page: str = Form(""), website: str = Form(""), user: Optional[User] = Depends(deps.get_current_user_optional), db: Session = Depends(get_db)):
    try:
        note = support_service.submit(
            user=user, email=email, category=category, message=message,
            page=from_page, user_agent=request.headers.get("user-agent", ""),
            client_ip=_client_ip(request), honeypot=website,
        )
    except support_service.SupportError as exc:
        return deps.render(request, "support.html", {
            "categories": support_service.CATEGORIES, "from_page": from_page,
            "support_email": support_service.settings.support_email, "max_message": support_service.MAX_MESSAGE,
            "err": str(exc), "sent_message": message, "sent_category": category, "sent_email": email,
        }, status_code=400, db=db)
    return deps.render(request, "support.html", {
        "categories": support_service.CATEGORIES, "from_page": "",
        "support_email": support_service.settings.support_email, "max_message": support_service.MAX_MESSAGE,
        "msg": note, "done": True,
    }, db=db)
