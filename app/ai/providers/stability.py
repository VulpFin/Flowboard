# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Stability AI adapter (image generation only)."""
from __future__ import annotations

from typing import Dict, List

import httpx

from ..base import ProviderAdapter
from ..types import ImageResult, ModelInfo


class StabilityAdapter(ProviderAdapter):
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.secrets.get('api_key', '')}", "Accept": "application/json"}

    def validate_credentials(self) -> str:
        data = self._request("GET", "/v1/user/balance")
        credits = data.get("credits")
        return f"OK - balance {credits:.2f} credits" if isinstance(credits, (int, float)) else "OK"

    def list_models(self) -> List[ModelInfo]:
        return self._finish_models(list(self.spec.fallback_models))

    def image_generation(self, prompt: str, model: str, **kwargs) -> ImageResult:
        # v2beta endpoints accept multipart form data and return base64 JSON.
        if model in ("sd3", "sd3.5-large", "sd3.5-medium", "sd3.5-large-turbo"):
            url = f"{self.base_url}/v2beta/stable-image/generate/sd3"
            form = {"prompt": prompt, "output_format": "png", "model": model if model != "sd3" else "sd3.5-large"}
        else:
            url = f"{self.base_url}/v2beta/stable-image/generate/{model}"
            form = {"prompt": prompt, "output_format": "png"}
        for k in ("aspect_ratio", "negative_prompt", "seed"):
            if kwargs.get(k) is not None:
                form[k] = str(kwargs[k])
        try:
            resp = self.http.post(url, data=form, files={"none": ("", b"")}, headers=self._headers())
        except httpx.HTTPError as exc:
            raise self.map_http_error(httpx.Response(503, text=str(exc)))
        data = self._handle_response(resp)
        import base64

        images = [base64.b64decode(data["image"])] if data.get("image") else []
        return ImageResult(images=images, model=model)
