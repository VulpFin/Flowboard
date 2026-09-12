# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Cohere v2 API adapter (chat + embeddings)."""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List

from ..base import ProviderAdapter
from ..types import ChatRequest, ChatResult, EmbeddingResult, ModelInfo, Usage


class CohereAdapter(ProviderAdapter):
    def _headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.secrets.get('api_key', '')}"}

    def list_models(self) -> List[ModelInfo]:
        data = self._request("GET", "/v1/models", params={"page_size": 100})
        models = []
        for m in data.get("models", []):
            eps = m.get("endpoints") or []
            tags = ["embedding"] if "embed" in eps and "chat" not in eps else []
            if "chat" not in eps and "embed" not in eps:
                continue
            models.append(ModelInfo(id=m["name"], context_window=m.get("context_length"), tags=tags))
        return self._finish_models(models or list(self.spec.fallback_models))

    def _build_body(self, req: ChatRequest, stream: bool = False) -> Dict[str, Any]:
        messages = []
        for m in req.messages:
            if m.role == "tool":
                messages.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
            elif m.role == "assistant" and m.tool_calls:
                messages.append({"role": "assistant", "tool_plan": m.content or "", "tool_calls": [{"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc.get("arguments") or {})}} for tc in m.tool_calls]})
            else:
                messages.append({"role": m.role, "content": m.content})
        body: Dict[str, Any] = {"model": req.model, "messages": messages, "stream": stream}
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.max_tokens:
            body["max_tokens"] = req.max_tokens
        if req.tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in req.tools]
        elif req.json_schema:
            body["response_format"] = {"type": "json_object", "json_schema": req.json_schema}
        elif req.json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def chat(self, request: ChatRequest) -> ChatResult:
        data = self._request("POST", "/v2/chat", json_body=self._build_body(request))
        msg = data.get("message") or {}
        text = "".join(p.get("text", "") for p in msg.get("content") or [] if p.get("type") == "text")
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            tool_calls.append({"id": tc.get("id"), "name": fn.get("name"), "arguments": args})
        tokens = ((data.get("usage") or {}).get("tokens")) or {}
        return ChatResult(content=text, tool_calls=tool_calls, usage=Usage(tokens.get("input_tokens"), tokens.get("output_tokens")), model=request.model, finish_reason=data.get("finish_reason", "") or "", raw=data)

    def stream_chat(self, request: ChatRequest) -> Iterator[str]:
        with self.http.stream("POST", f"{self.base_url}/v2/chat", json=self._build_body(request, stream=True), headers=self._headers()) as resp:
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
                if ev.get("type") == "content-delta":
                    txt = (((ev.get("delta") or {}).get("message") or {}).get("content") or {}).get("text")
                    if txt:
                        yield txt

    def embeddings(self, texts: List[str], model: str) -> EmbeddingResult:
        data = self._request("POST", "/v2/embed", json_body={"model": model, "texts": texts, "input_type": "search_document", "embedding_types": ["float"]})
        vectors = (data.get("embeddings") or {}).get("float") or []
        return EmbeddingResult(vectors=vectors, model=model)
