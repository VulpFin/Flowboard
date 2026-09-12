# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Replicate adapter.

Replicate runs arbitrary models through a predictions API.  We support:
  * validation (GET /account)
  * a curated model list (the public catalogue is enormous; `list_models`
    returns the curated fallback list plus anything the user pins in the
    provider config `models` field)
  * image generation for image models (predictions with `prompt`)
  * chat for language models that accept a `prompt` input (Replicate also
    exposes an OpenAI-compatible endpoint for some models; users who want that
    can add an "OpenAI-compatible (custom)" provider pointing at it).
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from ..base import ProviderAdapter
from ..types import ChatMessage, ChatRequest, ChatResult, ImageResult, ModelInfo, ProviderUnavailableError, Usage


class ReplicateAdapter(ProviderAdapter):
    def _headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.secrets.get('api_key', '')}", "Prefer": "wait=60"}

    def validate_credentials(self) -> str:
        data = self._request("GET", "/v1/account")
        return f"OK - account {data.get('username', '')}".strip()

    def list_models(self) -> List[ModelInfo]:
        models = list(self.spec.fallback_models)
        extra = self.config.get("models")
        if isinstance(extra, str):
            extra = [x.strip() for x in extra.split(",") if x.strip()]
        for mid in extra or []:
            if not any(m.id == mid for m in models):
                models.append(ModelInfo(id=mid))
        return self._finish_models(models)

    def _run(self, model: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
        owner_name, _, version = model.partition(":")
        if version:
            data = self._request("POST", "/v1/predictions", json_body={"version": version, "input": inputs})
        else:
            data = self._request("POST", f"/v1/models/{owner_name}/predictions", json_body={"input": inputs})
        deadline = time.time() + 180
        while data.get("status") in ("starting", "processing") and time.time() < deadline:
            time.sleep(1.5)
            data = self._request("GET", data["urls"]["get"], absolute=True)
        if data.get("status") != "succeeded":
            raise ProviderUnavailableError(f"prediction {data.get('status')}: {data.get('error') or ''}", provider=self.spec.id)
        return data

    def chat(self, request: ChatRequest) -> ChatResult:
        system = "\n".join(m.content for m in request.messages if m.role == "system")
        prompt = "\n".join(f"{m.role}: {m.content}" for m in request.messages if m.role != "system")
        inputs: Dict[str, Any] = {"prompt": prompt}
        if system:
            inputs["system_prompt"] = system
        if request.max_tokens:
            inputs["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            inputs["temperature"] = request.temperature
        data = self._run(request.model, inputs)
        out = data.get("output")
        text = "".join(out) if isinstance(out, list) else str(out or "")
        metrics = data.get("metrics") or {}
        return ChatResult(content=text, usage=Usage(metrics.get("input_token_count"), metrics.get("output_token_count")), model=request.model, raw=data)

    def image_generation(self, prompt: str, model: str, **kwargs) -> ImageResult:
        inputs = {"prompt": prompt}
        inputs.update({k: v for k, v in kwargs.items() if v is not None})
        data = self._run(model, inputs)
        out = data.get("output")
        urls = out if isinstance(out, list) else [out] if isinstance(out, str) else []
        images = []
        for u in urls[:4]:
            try:
                r = self.http.get(u)
                if r.status_code < 400:
                    images.append(r.content)
            except Exception:
                continue
        return ImageResult(images=images, urls=[u for u in urls if isinstance(u, str)], model=model)
