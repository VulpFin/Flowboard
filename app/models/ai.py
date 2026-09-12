# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""AI provider credentials, usage records and proposed change sets."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .common import TimestampMixin, UUIDPrimaryKeyMixin, utcnow


class AIProviderCredential(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per (user, provider).  `secret_blob` is the AES-256-GCM
    ciphertext of a JSON document holding every secret field of the provider's
    credential schema (api_key, org id, ...).  Non-secret configuration
    (base_url, default model) is stored in clear in `config_json`.
    """

    __tablename__ = "ai_provider_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_ai_cred_user_provider"),
        Index("ix_ai_cred_user", "user_id"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    secret_blob: Mapped[bytes] = mapped_column(nullable=False)  # versioned AES-GCM envelope
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    secret_hint: Mapped[str] = mapped_column(String(32), default="", nullable=False)  # "sk-••••4X2q"
    config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)  # base_url, org, project...
    default_model: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_validated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    validation_status: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)  # unknown|valid|invalid|error
    validation_message: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    models_cache_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    models_cached_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AIUsageRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "ai_usage"
    __table_args__ = (
        Index("ix_ai_usage_user_time", "user_id", "created_at"),
        Index("ix_ai_usage_board", "board_id"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    board_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    operation: Mapped[str] = mapped_column(String(48), default="chat", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    estimated_cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_kind: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    error_message: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    request_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)  # optional, no secrets
    response_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)


class AIChangeSet(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A proposal produced by the assistant that the user must review.

    `operations_json` is a list of validated operations
    (create_task / update_task / complete_task / delete_task / move_task ...).
    Nothing touches the board until the user approves.
    """

    __tablename__ = "ai_change_sets"
    __table_args__ = (Index("ix_changeset_board_status", "board_id", "status"),)

    board_id: Mapped[str] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    model_ref: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    request_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    assistant_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    operations_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="proposed", nullable=False)  # proposed|applied|rejected|partial|expired
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    result_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
