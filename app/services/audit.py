# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tiny, non-secret audit trail for privileged operator actions."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import AdminAuditEvent, User


def record(db: Session, *, actor: User, action: str, subject_type: str, subject_id: str, detail: str = "") -> None:
    db.add(AdminAuditEvent(actor_id=actor.id, action=action[:64], subject_type=subject_type[:32], subject_id=str(subject_id)[:64], detail=detail[:500]))
    db.flush()
