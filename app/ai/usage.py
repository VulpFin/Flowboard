# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""AI usage tracking (metadata only - never prompts with secrets, never keys)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AIUsageRecord, User, utcnow
from .registry import estimate_cost


def record_usage(
    db: Session,
    user: User,
    *,
    provider: str,
    model: str,
    operation: str,
    latency_ms: int,
    board_id: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    success: bool = True,
    error_kind: str = "",
    error_message: str = "",
    request_summary: str = "",
    response_summary: str = "",
) -> AIUsageRecord:
    rec = AIUsageRecord(
        user_id=user.id,
        board_id=board_id,
        provider=provider,
        model=model,
        operation=operation,
        latency_ms=int(latency_ms),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=estimate_cost(provider, model, input_tokens, output_tokens) if success else None,
        success=success,
        error_kind=error_kind[:40],
        error_message=(error_message or "")[:300],
        request_summary=(request_summary if settings.FLOWBOARD_AI_LOG_PROMPTS else "")[:4000],
        response_summary=(response_summary if settings.FLOWBOARD_AI_LOG_PROMPTS else "")[:4000],
    )
    db.add(rec)
    db.flush()
    return rec


def recent_usage(db: Session, user: User, limit: int = 100) -> List[AIUsageRecord]:
    return list(db.scalars(select(AIUsageRecord).where(AIUsageRecord.user_id == user.id).order_by(AIUsageRecord.created_at.desc()).limit(limit)))


def usage_summary(db: Session, user: User, days: int = 30) -> Dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    rows = db.execute(
        select(
            AIUsageRecord.provider,
            AIUsageRecord.model,
            func.count(AIUsageRecord.id),
            func.sum(AIUsageRecord.input_tokens),
            func.sum(AIUsageRecord.output_tokens),
            func.sum(AIUsageRecord.estimated_cost_usd),
            func.sum(func.cast(AIUsageRecord.success == False, __import__("sqlalchemy").Integer)),  # noqa: E712
            func.avg(AIUsageRecord.latency_ms),
        )
        .where(AIUsageRecord.user_id == user.id, AIUsageRecord.created_at >= since)
        .group_by(AIUsageRecord.provider, AIUsageRecord.model)
        .order_by(AIUsageRecord.provider, AIUsageRecord.model)
    ).all()
    by_model = [
        {
            "provider": r[0], "model": r[1], "calls": r[2] or 0, "input_tokens": r[3] or 0, "output_tokens": r[4] or 0,
            "cost": float(r[5] or 0.0), "errors": int(r[6] or 0), "avg_latency_ms": int(r[7] or 0),
        }
        for r in rows
    ]
    total_cost = sum(x["cost"] for x in by_model)
    by_provider: Dict[str, Dict[str, Any]] = {}
    for x in by_model:
        p = by_provider.setdefault(x["provider"], {"calls": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0, "errors": 0})
        for k in ("calls", "cost", "input_tokens", "output_tokens", "errors"):
            p[k] += x[k]
    return {"days": days, "by_model": by_model, "by_provider": by_provider, "total_cost": total_cost, "total_calls": sum(x["calls"] for x in by_model)}
