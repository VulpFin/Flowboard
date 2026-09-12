# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Anthropic Messages API adapter."""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List

from ..base import ProviderAdapter
from ..types import ChatMessage, ChatRequest, ChatResult, ModelInfo, Usage

API_VERSION = "2023-06-01"


class AnthropicAdapter(ProviderAdapter):
    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.secrets.get("api_key", ""),
            "anthropic-version": API_VERSION,
        }

    def list_models(self) -> List[ModelInfo]:
        data = self._request("GET", "/models", params={"limit": 100})
        models = [ModelInfo(id=m["id"], name=m.get("display_name", "")) for m in data.get("data", []) if m.get("id")]
        return self._finish_models(models or list(self.spec.fallback_models))

    def _build_body(self, req: ChatRequest, stream: bool = False) -> Dict[str, Any]:
        system_parts = [m.content for m in req.messages if m.role == "system" and m.content]
        messages: List[Dict[str, Any]] = []
        for m in req.messages:
            if m.role == "system":
                continue
            if m.role == "tool":
                messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}]})
            elif m.role == "assistant" and m.tool_calls:
                parts: List[Dict[str, Any]] = []
                if m.content:
                    parts.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    parts.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc.get("arguments") or {}})
                messages.append({"role": "assistant", "content": parts})
            else:
                messages.append({"role": m.role, "content": m.content})
        # Anthropic requires alternating roles; merge consecutive same-role messages.
        merged: List[Dict[str, Any]] = []
        for msg in messages:
            if merged and merged[-1]["role"] == msg["role"]:
                prev = merged[-1]
                pc = prev["content"] if isinstance(prev["content"], list) else [{"type": "text", "text": prev["content"]}]
                nc = msg["content"] if isinstance(msg["content"], list) else [{"type": "text", "text": msg["content"]}]
                prev["content"] = pc + nc
            else:
                merged.append(msg)
        body: Dict[str, Any] = {"model": req.model, "messages": merged, "max_tokens": req.max_tokens or 1024}
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.tools:
            body["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in req.tools]
            if req.tool_choice == "required":
                body["tool_choice"] = {"type": "any"}
            elif req.tool_choice and req.tool_choice not in ("auto", "none"):
                body["tool_choice"] = {"type": "tool", "name": req.tool_choice}
        if stream:
            body["stream"] = True
        body.update(req.extra or {})
        return body

    def chat(self, request: ChatRequest) -> ChatResult:
        data = self._request("POST", "/messages", json_body=self._build_body(request))
        text, tool_calls = "", []
        for part in data.get("content", []):
            if part.get("type") == "text":
                text += part.get("text", "")
            elif part.get("type") == "tool_use":
                tool_calls.append({"id": part.get("id"), "name": part.get("name"), "arguments": part.get("input") or {}})
        usage = data.get("usage") or {}
        return ChatResult(content=text, tool_calls=tool_calls, usage=Usage(usage.get("input_tokens"), usage.get("output_tokens")), model=data.get("model", ""), finish_reason=data.get("stop_reason", "") or "", raw=data)

    def stream_chat(self, request: ChatRequest) -> Iterator[str]:
        with self.http.stream("POST", f"{self.base_url}/messages", json=self._build_body(request, stream=True), headers=self._headers()) as resp:
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
                if ev.get("type") == "content_block_delta":
                    d = ev.get("delta") or {}
                    if d.get("type") == "text_delta" and d.get("text"):
                        yield d["text"]
