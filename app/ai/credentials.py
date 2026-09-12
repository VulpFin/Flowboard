# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Credential storage service.

Rules enforced here (and only here):
  * secrets are encrypted with AES-256-GCM, AAD = "ai-credential:<user_id>:<provider>"
    so a blob can never be decrypted for another user/provider
  * plaintext secrets never leave this module except into an adapter instance
  * every function takes the owning `User`; there is no "get by id" without
    the user, which makes cross-user access impossible by construction
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AIProviderCredential, User, utcnow
from ..security.crypto import get_cipher, mask_secret
from .base import ProviderAdapter
from .registry import build_adapter, get_spec
from .types import ModelInfo, ProviderError

MODELS_CACHE_TTL = timedelta(hours=12)


def _aad(user_id: str, provider: str) -> str:
    return f"ai-credential:{user_id}:{provider}"


def list_credentials(db: Session, user: User) -> List[AIProviderCredential]:
    return list(db.scalars(select(AIProviderCredential).where(AIProviderCredential.user_id == user.id).order_by(AIProviderCredential.provider)))


def get_credential(db: Session, user: User, provider: str) -> Optional[AIProviderCredential]:
    return db.scalar(select(AIProviderCredential).where(AIProviderCredential.user_id == user.id, AIProviderCredential.provider == provider))


def get_credential_by_id(db: Session, user: User, cred_id: str) -> Optional[AIProviderCredential]:
    cred = db.get(AIProviderCredential, cred_id)
    if cred is None or cred.user_id != user.id:
        return None
    return cred


def config_of(cred: AIProviderCredential) -> Dict[str, Any]:
    try:
        return json.loads(cred.config_json or "{}")
    except Exception:
        return {}


def upsert_credential(
    db: Session,
    user: User,
    provider: str,
    *,
    secrets: Dict[str, str],
    config: Optional[Dict[str, Any]] = None,
    label: str = "",
) -> AIProviderCredential:
    """Create or update.  Secret fields that are submitted empty keep their
    previous value (so the user can edit config without re-entering the key)."""
    spec = get_spec(provider)
    cred = get_credential(db, user, provider)
    cipher = get_cipher()
    existing: Dict[str, str] = {}
    if cred is not None:
        try:
            existing = cipher.decrypt_json(cred.secret_blob, _aad(user.id, provider))
        except Exception:
            existing = {}
    merged = dict(existing)
    for f in spec.credential_fields:
        value = (secrets.get(f.key) or "").strip()
        if value:
            merged[f.key] = value
    for f in spec.credential_fields:
        if f.required and not merged.get(f.key):
            raise ValueError(f"{f.label} is required")
    cfg = dict(config_of(cred)) if cred else {}
    for f in spec.config_fields:
        if config is not None and f.key in config:
            v = (config.get(f.key) or "").strip() if isinstance(config.get(f.key), str) else config.get(f.key)
            if v:
                cfg[f.key] = v
            else:
                cfg.pop(f.key, None)
    for f in spec.config_fields:
        if f.required and not cfg.get(f.key):
            raise ValueError(f"{f.label} is required")
    blob, version = cipher.encrypt_json(merged, _aad(user.id, provider))
    primary = merged.get("api_key", "") or next(iter(merged.values()), "")
    if cred is None:
        cred = AIProviderCredential(user_id=user.id, provider=provider, secret_blob=blob, key_version=version)
        db.add(cred)
    else:
        cred.secret_blob = blob
        cred.key_version = version
    cred.secret_hint = mask_secret(primary)
    cred.config_json = json.dumps(cfg)
    cred.label = (label or cfg.get("label") or "")[:80]
    cred.validation_status = "unknown"
    cred.validation_message = ""
    cred.enabled = True
    db.flush()
    return cred


def delete_credential(db: Session, user: User, cred: AIProviderCredential) -> None:
    assert cred.user_id == user.id
    db.delete(cred)
    db.flush()


def decrypt_secrets(cred: AIProviderCredential) -> Dict[str, str]:
    return get_cipher().decrypt_json(cred.secret_blob, _aad(cred.user_id, cred.provider))


def adapter_for(cred: AIProviderCredential) -> ProviderAdapter:
    return build_adapter(cred.provider, decrypt_secrets(cred), config_of(cred))


def rotate_if_needed(db: Session, cred: AIProviderCredential) -> bool:
    cipher = get_cipher()
    if not cipher.needs_rotation(cred.secret_blob):
        return False
    secrets = decrypt_secrets(cred)
    cred.secret_blob, cred.key_version = cipher.encrypt_json(secrets, _aad(cred.user_id, cred.provider))
    db.flush()
    return True


def test_credential(db: Session, cred: AIProviderCredential) -> Dict[str, Any]:
    """Validate against the provider and refresh the model cache."""
    adapter = adapter_for(cred)
    try:
        message = adapter.validate_credentials()
        try:
            models = adapter.list_models()
            cred.models_cache_json = json.dumps([m.to_dict() for m in models])
            cred.models_cached_at = utcnow()
        except ProviderError:
            models = []
        cred.validation_status = "valid"
        cred.validation_message = message[:300]
        cred.last_validated_at = utcnow()
        db.flush()
        return {"ok": True, "message": message, "models": len(models)}
    except ProviderError as exc:
        cred.validation_status = "invalid" if exc.kind == "auth" else "error"
        cred.validation_message = exc.friendly()[:300]
        cred.last_validated_at = utcnow()
        db.flush()
        return {"ok": False, "message": exc.friendly(), "kind": exc.kind}


def cached_models(cred: AIProviderCredential) -> List[ModelInfo]:
    try:
        return [ModelInfo.from_dict(m) for m in json.loads(cred.models_cache_json or "[]")]
    except Exception:
        return []


def models_for_credential(db: Session, cred: AIProviderCredential, *, refresh: bool = False) -> List[ModelInfo]:
    stale = cred.models_cached_at is None or (utcnow() - cred.models_cached_at) > MODELS_CACHE_TTL
    if refresh or (stale and cred.enabled):
        try:
            models = adapter_for(cred).list_models()
            cred.models_cache_json = json.dumps([m.to_dict() for m in models])
            cred.models_cached_at = utcnow()
            db.flush()
            return models
        except ProviderError:
            pass
    cached = cached_models(cred)
    if cached:
        return cached
    spec = get_spec(cred.provider)
    return list(spec.fallback_models)
