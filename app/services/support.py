# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
""""Contact support" - the footer form that emails the operator.

Deliberately open to signed-out visitors: the person who cannot log in is
exactly the one who needs to report something.  That makes it a public
mail-sending endpoint, so it is kept boring and safe:

* CSRF like every other form, plus a honeypot field bots fill in;
* header values scrubbed of CR/LF (no header injection) and length-capped;
* the reporter's address goes in `Reply-To`, never in `From`, so replies work
  without letting anyone forge the sender;
* a per-user / per-IP hourly cap (`FLOWBOARD_SUPPORT_MAX_PER_HOUR`);
* diagnostics (version, page, browser) are attached from what the server
  already knows, not from anything the form can claim.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Dict, List, Optional, Tuple

from .. import __version__
from ..config import settings
from ..models import User
from . import mail as mail_service

log = logging.getLogger("flowboard.support")

CATEGORIES = [
    ("bug", "Something is broken"),
    ("question", "I need help using it"),
    ("feature", "Idea or request"),
    ("account", "Account or sign-in problem"),
    ("other", "Something else"),
]
CATEGORY_IDS = {c for c, _label in CATEGORIES}
MAX_MESSAGE = 5000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")

#: {key: [unix timestamps]} - per process, which is enough to stop a flood
_recent: Dict[str, List[float]] = {}


class SupportError(Exception):
    pass


def _rate_key(user: Optional[User], client_ip: str) -> str:
    return f"user:{user.id}" if user is not None else f"ip:{client_ip or 'unknown'}"


def rate_limited(key: str, *, now: Optional[float] = None) -> bool:
    now = now or time.time()
    window = [t for t in _recent.get(key, []) if now - t < 3600]
    _recent[key] = window
    return len(window) >= max(1, settings.FLOWBOARD_SUPPORT_MAX_PER_HOUR)


def _record(key: str, *, now: Optional[float] = None) -> None:
    _recent.setdefault(key, []).append(now or time.time())


def build_message(*, user: Optional[User], email: str, category: str, message: str, diagnostics: Dict[str, str]) -> Tuple[str, str]:
    label = dict(CATEGORIES).get(category, category)
    who = f"{user.display_name or user.username} <{user.email}> (id {user.id})" if user is not None else f"{email} (not signed in)"
    subject = f"[Flowboard support] {label} — {email}"
    lines = [
        f"Category: {label}",
        f"From: {who}",
        f"Reply to: {email}",
        "",
        message.strip(),
        "",
        "--",
    ]
    lines += [f"{k}: {v}" for k, v in diagnostics.items() if v]
    return subject, "\n".join(lines)


def submit(*, user: Optional[User], email: str, category: str, message: str, page: str = "", user_agent: str = "", client_ip: str = "", honeypot: str = "") -> str:
    """Send a support report.  Returns a message for the user, raises SupportError."""
    if honeypot.strip():
        log.info("support: honeypot filled from %s; dropping silently", client_ip)
        return "Thanks — your message has been sent."  # bots get a cheerful lie
    email = (email or (user.email if user is not None else "")).strip()[:254]
    if not EMAIL_RE.match(email):
        raise SupportError("Please give an email address we can reply to.")
    if category not in CATEGORY_IDS:
        category = "other"
    message = (message or "").strip()
    if len(message) < 10:
        raise SupportError("Please describe the problem in a sentence or two.")
    if len(message) > MAX_MESSAGE:
        raise SupportError(f"That is longer than {MAX_MESSAGE} characters — please trim it a little.")
    key = _rate_key(user, client_ip)
    if rate_limited(key):
        raise SupportError("You have sent several reports in the last hour; please wait a little before sending another.")

    diagnostics = {
        "Flowboard": __version__,
        "Page": page[:300],
        "Browser": user_agent[:300],
        "Client": client_ip[:64],
        "Account": (user.username if user is not None else "signed out"),
        "Timezone": (user.profile.timezone if user is not None and user.profile else ""),
    }
    subject, body = build_message(user=user, email=email, category=category, message=message, diagnostics=diagnostics)
    sent = mail_service.send_mail(settings.support_email, subject, body, reply_to=email)
    _record(key)
    if not sent:
        log.error("support: could not send a report from %s (SMTP not configured or refused)", email)
        raise SupportError("We could not send that just now. Please email " + settings.support_email + " directly.")
    return "Thanks — your message has been sent. We reply to " + email + "."
