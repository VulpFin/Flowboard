# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Small client for TG11 Accounts' first-party API (beyond plain OIDC):

* `fetch_vault(access_token)`  - the user's AI keys stored in their TG11 vault
  (needs the `tg11.ai` scope and a *trusted* client registration).
* `register_link(sub, legacy_id)` - tells TG11 which local Flowboard account a
  TG11 identity maps to (client-credentials basic auth).
* `import_vault(db, user, creds)` - copies vault entries into Flowboard's
  encrypted credential store (only providers Flowboard knows)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models import User, utcnow

TIMEOUT = 15.0


def _base() -> str:
    return settings.TG11_OIDC_ISSUER.rstrip("/")


def fetch_vault(access_token: str, http: Optional[httpx.Client] = None) -> List[Dict[str, Any]]:
    if not access_token or not settings.oidc_configured:
        return []
    client = http or httpx.Client(timeout=TIMEOUT)
    r = client.get(f"{_base()}/api/v1/ai/credentials", headers={"Authorization": f"Bearer {access_token}"})
    if r.status_code != 200:
        return []
    data = r.json()
    return list(data.get("credentials") or []) if isinstance(data, dict) else []


def register_link(sub: str, legacy_id: str, *, source: str = "oidc_login", http: Optional[httpx.Client] = None) -> bool:
    if not settings.oidc_configured or not settings.TG11_OIDC_CLIENT_SECRET:
        return False
    client = http or httpx.Client(timeout=TIMEOUT)
    try:
        r = client.post(f"{_base()}/api/v1/links", json={"sub": sub, "legacy_id": legacy_id, "local_uuid": legacy_id, "source": source}, auth=(settings.TG11_OIDC_CLIENT_ID, settings.TG11_OIDC_CLIENT_SECRET))
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def import_vault(db: Session, user: User, creds: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Upsert each vault entry into Flowboard's credential store.  Existing
    Flowboard credentials for the same provider are replaced (the TG11 vault
    is the source of truth when the user chooses to sync)."""
    from ..ai import credentials as cred_service
    from ..ai.registry import PROVIDERS

    imported, skipped = [], []
    for c in creds:
        provider = str(c.get("provider") or "")
        secrets = c.get("secrets") or {}
        if provider not in PROVIDERS or not isinstance(secrets, dict) or not any(secrets.values()):
            skipped.append(provider or "?")
            continue
        try:
            cred_service.upsert_credential(db, user, provider, secrets={k: str(v) for k, v in secrets.items()}, config={k: str(v) for k, v in (c.get("config") or {}).items()}, label=(str(c.get("label") or "").strip() or "from TG11 vault")[:80])
            imported.append(provider)
        except Exception:
            skipped.append(provider)
    if user.profile is not None:
        user.profile.tg11_vault_synced_at = utcnow()
    db.flush()
    return {"imported": imported, "skipped": skipped}
