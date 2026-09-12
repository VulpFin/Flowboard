# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Azure OpenAI via the OpenAI-compatible `/openai/v1` surface.

Base URL is the resource endpoint (https://<resource>.openai.azure.com); the
model field is the *deployment name*.  Authentication uses the `api-key`
header instead of a bearer token."""
from __future__ import annotations

from typing import Dict, List

from ..types import ModelInfo
from .openai_compat import OpenAICompatAdapter


class AzureOpenAIAdapter(OpenAICompatAdapter):
    def __init__(self, spec, secrets, config, **kw):
        super().__init__(spec, secrets, config, **kw)
        base = (config or {}).get("base_url") or ""
        if base and "/openai/v1" not in base:
            self.base_url = base.rstrip("/") + "/openai/v1"

    def _headers(self) -> Dict[str, str]:
        h = super()._headers()
        h.pop("Authorization", None)
        h["api-key"] = self.secrets.get("api_key", "")
        return h

    def list_models(self) -> List[ModelInfo]:
        # Deployments are user-named; the user lists them in config ("deployments").
        names = [d.strip() for d in str(self.config.get("deployments") or "").replace("\n", ",").split(",") if d.strip()]
        models = [ModelInfo(id=n, name=n, owned_by="azure") for n in names]
        if not models:
            try:
                models = super().list_models()
            except Exception:
                models = []
        return self._finish_models(models)

    def validate_credentials(self) -> str:
        models = self.list_models()
        if not models:
            raise ValueError("List at least one deployment name")
        return f"OK - {len(models)} deployment(s) configured"
