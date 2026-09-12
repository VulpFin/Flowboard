# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Google Gemini (Generative Language API) adapter."""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, Iterator, List

from ..base import ProviderAdapter
from ..types import ChatRequest, ChatResult, EmbeddingResult, ImageResult, ModelInfo, Usage


class GeminiAdapter(ProviderAdapter):
    def _headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json", "x-goog-api-key": self.secrets.get("api_key", "")}

    def list_models(self) -> List[ModelInfo]:
        data = self._request("GET", "/models", params={"pageSize": 200})
        models = []
        for m in data.get("models", []):
            name = m.get("name", "")
            mid = name.split("/", 1)[1] if "/" in name else name
            methods = m.get("supportedGenerationMethods") or []
            tags = []
            if "embedContent" in methods:
                tags = ["embedding"]
            elif "generateContent" not in methods and "predict" not in methods:
                continue
            models.append(ModelInfo(id=mid, name=m.get("displayName", ""), context_window=m.get("inputTokenLimit"), tags=tags))
        return self._finish_models(models or list(self.spec.fallback_models))

    def _build_body(self, req: ChatRequest) -> Dict[str, Any]:
        system = "\n\n".join(m.content for m in req.messages if m.role == "system" and m.content)
        contents: List[Dict[str, Any]] = []
        for m in req.messages:
            if m.role == "system":
                continue
            if m.role == "tool":
                contents.append({"role": "user", "parts": [{"functionResponse": {"name": m.name or "tool", "response": {"result": m.content}}}]})
            elif m.role == "assistant":
                parts: List[Dict[str, Any]] = []
                if m.content:
                    parts.append({"text": m.content})
                for tc in m.tool_calls or []:
                    parts.append({"functionCall": {"name": tc["name"], "args": tc.get("arguments") or {}}})
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            else:
                contents.append({"role": "user", "parts": [{"text": m.content}]})
        body: Dict[str, Any] = {"contents": contents, "generationConfig": {}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if req.temperature is not None:
            body["generationConfig"]["temperature"] = req.temperature
        if req.max_tokens:
            body["generationConfig"]["maxOutputTokens"] = req.max_tokens
        if req.tools:
            body["tools"] = [{"functionDeclarations": [{"name": t.name, "description": t.description, "parameters": _strip_schema(t.parameters)} for t in req.tools]}]
            if req.tool_choice == "required":
                body["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}
            elif req.tool_choice and req.tool_choice not in ("auto", "none"):
                body["toolConfig"] = {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [req.tool_choice]}}
        elif req.json_schema:
            body["generationConfig"]["responseMimeType"] = "application/json"
            body["generationConfig"]["responseSchema"] = _strip_schema(req.json_schema)
        elif req.json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        return body

    def chat(self, request: ChatRequest) -> ChatResult:
        data = self._request("POST", f"/models/{request.model}:generateContent", json_body=self._build_body(request))
        cands = data.get("candidates") or []
        text, tool_calls = "", []
        if cands:
            for i, part in enumerate((cands[0].get("content") or {}).get("parts") or []):
                if "text" in part:
                    text += part["text"]
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    tool_calls.append({"id": f"call_{i}", "name": fc.get("name"), "arguments": fc.get("args") or {}})
        usage = data.get("usageMetadata") or {}
        return ChatResult(content=text, tool_calls=tool_calls, usage=Usage(usage.get("promptTokenCount"), usage.get("candidatesTokenCount")), model=request.model, finish_reason=(cands[0].get("finishReason", "") if cands else ""), raw=data)

    def stream_chat(self, request: ChatRequest) -> Iterator[str]:
        url = f"{self.base_url}/models/{request.model}:streamGenerateContent?alt=sse"
        with self.http.stream("POST", url, json=self._build_body(request), headers=self._headers()) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise self.map_http_error(resp)
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except ValueError:
                    continue
                for c in ev.get("candidates") or []:
                    for part in (c.get("content") or {}).get("parts") or []:
                        if part.get("text"):
                            yield part["text"]

    def embeddings(self, texts: List[str], model: str) -> EmbeddingResult:
        data = self._request("POST", f"/models/{model}:batchEmbedContents", json_body={"requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t}]}} for t in texts]})
        return EmbeddingResult(vectors=[e.get("values", []) for e in data.get("embeddings", [])], model=model)

    def image_generation(self, prompt: str, model: str, **kwargs) -> ImageResult:
        # Gemini image-capable models return inline image parts from generateContent.
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]}}
        data = self._request("POST", f"/models/{model}:generateContent", json_body=body)
        images = []
        for c in data.get("candidates") or []:
            for part in (c.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData")
                if inline and inline.get("data"):
                    images.append(base64.b64decode(inline["data"]))
        return ImageResult(images=images, model=model)


def _strip_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Gemini rejects some JSON-schema keywords; drop them recursively."""
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k in ("additionalProperties", "$schema", "default", "examples", "title"):
            continue
        if isinstance(v, dict):
            out[k] = _strip_schema(v)
        elif isinstance(v, list):
            out[k] = [_strip_schema(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out
