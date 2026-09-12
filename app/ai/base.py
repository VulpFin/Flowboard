# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Provider adapter interface.

A provider is described by a `ProviderSpec` (static metadata + credential
schema) and implemented by a `ProviderAdapter` subclass which receives the
decrypted credentials and per-user configuration at construction time.

Adapters implement only what the provider supports; unsupported operations
raise `UnsupportedCapabilityError` from the base class.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

import httpx

from .types import (
    AuthenticationError,
    BadRequestError,
    Capabilities,
    ChatRequest,
    ChatResult,
    CredentialField,
    EmbeddingResult,
    ImageResult,
    InsufficientCreditsError,
    ModelInfo,
    ModelNotFoundError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
    UnsupportedCapabilityError,
)

DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=15.0)


@dataclass
class ProviderSpec:
    id: str
    name: str
    docs_url: str = ""
    console_url: str = ""  # where the user gets a key
    credential_fields: List[CredentialField] = field(default_factory=list)
    config_fields: List[CredentialField] = field(default_factory=list)  # non-secret (base_url, org...)
    default_base_url: str = ""
    models_url: str = ""  # absolute URL override for model listing (e.g. GitHub Models catalog)
    capabilities: Capabilities = field(default_factory=Capabilities)
    adapter: str = "openai_compat"  # registry key of the adapter class
    family_rules: List[tuple] = field(default_factory=list)  # [(regex, family label)]
    hide_model_patterns: List[str] = field(default_factory=list)
    fallback_models: List[ModelInfo] = field(default_factory=list)  # used when list_models unsupported/fails
    notes: str = ""
    status: str = "supported"  # supported | limited | legacy

    def family_for(self, model_id: str) -> str:
        for pattern, label in self.family_rules:
            if re.search(pattern, model_id, re.IGNORECASE):
                return label
        return "Other"

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "docs_url": self.docs_url,
            "console_url": self.console_url,
            "capabilities": self.capabilities.to_dict(),
            "credential_fields": [f.__dict__ for f in self.credential_fields],
            "config_fields": [f.__dict__ for f in self.config_fields],
            "default_base_url": self.default_base_url,
            "notes": self.notes,
            "status": self.status,
        }


class ProviderAdapter:
    """Base adapter.  Subclasses override the operations they support."""

    spec: ProviderSpec

    def __init__(self, spec: ProviderSpec, secrets: Dict[str, str], config: Dict[str, Any], *, http: Optional[httpx.Client] = None):
        self.spec = spec
        self.secrets = secrets
        self.config = config or {}
        self.base_url = (self.config.get("base_url") or spec.default_base_url).rstrip("/")
        self._http = http

    # ---- capabilities -------------------------------------------------
    def get_capabilities(self, model: Optional[str] = None) -> Capabilities:
        caps = Capabilities(**self.spec.capabilities.to_dict())
        if model:
            info = self.describe_model(model)
            if info and info.capabilities:
                caps = info.capabilities
        return caps

    def describe_model(self, model_id: str) -> Optional[ModelInfo]:
        for m in self.spec.fallback_models:
            if m.id == model_id:
                return m
        return None

    # ---- required ----------------------------------------------------
    def validate_credentials(self) -> str:
        """Return a short human-readable success message or raise ProviderError."""
        models = self.list_models()
        return f"OK - {len(models)} models visible"

    def list_models(self) -> List[ModelInfo]:
        if self.spec.fallback_models:
            return list(self.spec.fallback_models)
        raise UnsupportedCapabilityError("model enumeration not supported", provider=self.spec.id)

    def chat(self, request: ChatRequest) -> ChatResult:
        raise UnsupportedCapabilityError("chat not supported", provider=self.spec.id)

    def stream_chat(self, request: ChatRequest) -> Iterator[str]:
        # Default: no true streaming -> yield the full answer once.
        yield self.chat(request).content

    def embeddings(self, texts: List[str], model: str) -> EmbeddingResult:
        raise UnsupportedCapabilityError("embeddings not supported", provider=self.spec.id)

    def image_generation(self, prompt: str, model: str, **kwargs) -> ImageResult:
        raise UnsupportedCapabilityError("image generation not supported", provider=self.spec.id)

    # ---- helpers -------------------------------------------------------
    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=False)
        return self._http

    def _headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json"}

    def _request(self, method: str, path: str, *, json_body: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None, absolute: bool = False) -> Any:
        url = path if absolute or path.startswith("http") else f"{self.base_url}{path}"
        hdrs = self._headers()
        if headers:
            hdrs.update(headers)
        try:
            resp = self.http.request(method, url, json=json_body, headers=hdrs, params=params)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError(f"timeout talking to {self.spec.name}", provider=self.spec.id) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"network error: {exc.__class__.__name__}", provider=self.spec.id) from exc
        return self._handle_response(resp)

    def _handle_response(self, resp: httpx.Response) -> Any:
        if resp.status_code < 400:
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}
        raise self.map_http_error(resp)

    def map_http_error(self, resp: httpx.Response) -> ProviderError:
        status = resp.status_code
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        msg = _extract_error_message(payload) or resp.text[:300]
        lowered = (msg or "").lower()
        kw = dict(provider=self.spec.id, status=status)
        if status in (401, 403):
            return AuthenticationError(msg, **kw)
        if status == 402 or "insufficient" in lowered and ("credit" in lowered or "balance" in lowered or "quota" in lowered) or "exceeded your current quota" in lowered:
            return InsufficientCreditsError(msg, **kw)
        if status == 429:
            retry = resp.headers.get("retry-after")
            if "quota" in lowered or "billing" in lowered or "credit" in lowered:
                return InsufficientCreditsError(msg, **kw)
            return RateLimitedError(msg, retry_after=float(retry) if retry and retry.replace(".", "", 1).isdigit() else None, **kw)
        if status == 404 or ("model" in lowered and ("not found" in lowered or "does not exist" in lowered or "not exist" in lowered)):
            return ModelNotFoundError(msg, **kw)
        if status >= 500 or status in (408, 409):
            return ProviderUnavailableError(msg, **kw)
        return BadRequestError(msg, **kw)

    # ---- model post-processing --------------------------------------
    def _finish_models(self, models: List[ModelInfo]) -> List[ModelInfo]:
        out = []
        for m in models:
            if any(re.search(p, m.id, re.IGNORECASE) for p in self.spec.hide_model_patterns):
                continue
            if not m.family:
                m.family = self.spec.family_for(m.id)
            if not m.tags:
                m.tags = infer_tags(m.id, m.family)
            out.append(m)
        out.sort(key=lambda m: (m.family.lower(), m.id.lower()))
        return out


def _extract_error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        err = payload.get("error") or payload.get("message") or payload.get("detail")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or json.dumps(err)[:200])
        if isinstance(err, list) and err:
            return str(err[0])[:200]
        if err:
            return str(err)[:300]
    return ""


def infer_tags(model_id: str, family: str = "") -> List[str]:
    """Best-effort tags from the model id.  These are hints for the UI only and
    are overridden by anything the provider reports explicitly."""
    m = model_id.lower()
    tags: List[str] = []
    if any(k in m for k in ("embed", "embedding")):
        return ["embedding"]
    if any(k in m for k in ("dall-e", "image", "sd3", "stable-diffusion", "flux", "imagen", "gpt-image")):
        return ["image"]
    if any(k in m for k in ("whisper", "tts", "audio", "transcri")):
        return ["audio"]
    if re.search(r"(^|[-/:])o[1-9](-|$)|reason|think|r1|deepseek-reasoner|qwq|grok-.*(mini)?-?(reason|think)|gemini-.*thinking|claude-(opus|sonnet)-4|gpt-5", m):
        tags.append("reasoning")
    if any(k in m for k in ("mini", "flash", "haiku", "nano", "small", "8b", "instant", "lite", "turbo", "fast")):
        tags.append("fast")
    if any(k in m for k in ("vision", "gpt-4o", "gpt-4.1", "gpt-5", "claude-3", "claude-sonnet", "claude-opus", "claude-haiku", "gemini", "grok-2-vision", "grok-4", "llava", "pixtral", "vl")):
        tags.append("vision")
    return tags
