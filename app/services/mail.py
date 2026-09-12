# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Minimal SMTP mailer (password reset).  If SMTP is not configured the
message is logged instead so development still works."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from ..config import settings
from ..models import User

log = logging.getLogger("flowboard.mail")


def send_mail(to: str, subject: str, body: str) -> bool:
    msg = EmailMessage()
    msg["From"] = settings.EMAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if not settings.SMTP_HOST:
        log.warning("SMTP not configured; would send to %s: %s\n%s", to, subject, body)
        return False
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as s:
            if settings.SMTP_USE_TLS:
                s.starttls()
            if settings.SMTP_USER:
                s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception as exc:  # pragma: no cover - network
        log.error("mail send failed: %s", exc)
        return False


def send_board_invite(to: str, *, board, inviter: User, url: str, role: str, message: str = "") -> bool:
    who = inviter.display_name or inviter.username
    note = f"\n{who} says:\n\n  {message}\n" if message else ""
    return send_mail(
        to,
        f"{settings.FLOWBOARD_SITE_NAME}: {who} invited you to “{board.name}”",
        f"{who} ({inviter.email}) invited you to join the board “{board.name}” on "
        f"{settings.FLOWBOARD_SITE_NAME} as {role}.\n{note}\n"
        f"Open this link to accept or decline:\n\n{url}\n\n"
        f"The link works for 14 days and only for this email address. "
        f"If you did not expect this invitation you can ignore it.\n",
    )


def send_password_reset(user: User, link: str) -> bool:
    return send_mail(
        user.email,
        f"{settings.FLOWBOARD_SITE_NAME}: reset your password",
        f"Hi {user.display_name or user.username},\n\nSomeone (hopefully you) asked to reset your {settings.FLOWBOARD_SITE_NAME} password.\n"
        f"Open this link within 60 minutes to choose a new one:\n\n{link}\n\nIf you did not request this, ignore this email.\n",
    )
