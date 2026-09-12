# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Shared adapter for every provider exposing the OpenAI Chat Completions API
(OpenAI, xAI, Groq, Together, Mistral, OpenRouter, DeepSeek, Moonshot,
Cerebras, Perplexity, Novita, MiniMax, GitHub Models, ...).

Provider-specific quirks are expressed through `ProviderSpec` flags and a few
overridable hooks rather than subclasses per provider.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional

from ..base import ProviderAdapter
from ..types import ChatMessage, ChatRequest, ChatResult, EmbeddingResult, ImageResult, ModelInfo, StructuredOutputError, UnsupportedCapabilityError, Usage


class OpenAICompatAdapter(ProviderAdapter):
    models_path = "/models"
    chat_path = "/chat/completions"
    embeddings_path = "/embeddings"
    images_path = "/images/generations"

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Authorization": f"Bearer {self.secrets.get('api_key', '')}"}
        org = self.config.get("organization") or self.secrets.get("organization")
        proj = self.config.get("project") or self.secrets.get("project")
        if org:
            h["OpenAI-Organization"] = org
        if proj:
            h["OpenAI-Project"] = proj
        extra = self.config.get("extra_headers")
        if isinstance(extra, dict):
            h.update({str(k): str(v) for k, v in extra.items()})
        return h

    # ---- models --------------------------------------------------------
    def list_models(self) -> List[ModelInfo]:
        if not self.spec.capabilities.list_models:
            return self._finish_models(list(self.spec.fallback_models))
        data = self._request("GET", self.spec.models_url or self.models_path, absolute=bool(self.spec.models_url))
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = []
        models: List[ModelInfo] = []
        for it in items:
            if not isinstance(it, dict) or not it.get("id"):
                continue
            mid = str(it["id"])
            ctx = it.get("context_length") or it.get("context_window") or (it.get("top_provider") or {}).get("context_length")
            name = it.get("name") or it.get("display_name") or ""
            # OpenRouter reports modalities & supported params
            tags: List[str] = []
            arch = it.get("architecture") or {}
            if isinstance(arch, dict):
                mods = arch.get("input_modalities") or []
                if "image" in mods:
                    tags.append("vision")
                out_mods = arch.get("output_modalities") or []
                if "image" in out_mods:
                    tags.append("image")
            sp = it.get("supported_parameters") or []
            if "reasoning" in sp or "include_reasoning" in sp:
                tags.append("reasoning")
            models.append(ModelInfo(id=mid, name=name, context_window=int(ctx) if isinstance(ctx, (int, float)) else None, owned_by=str(it.get("owned_by", "")), tags=tags))
        if not models and self.spec.fallback_models:
            models = list(self.spec.fallback_models)
        return self._finish_models(models)

    # ---- chat ----------------------------------------------------------
    def _build_body(self, req: ChatRequest, stream: bool = False) -> Dict[str, Any]:
        body: Dict[str, Any] = {"model": req.model, "messages": [self._msg(m) for m in req.messages]}
        if req.temperature is not None and not self._is_reasoning_model(req.model):
            body["temperature"] = req.temperature
        if req.max_tokens:
            if self._uses_max_completion_tokens(req.model):
                body["max_completion_tokens"] = req.max_tokens
            else:
                body["max_tokens"] = req.max_tokens
        if req.tools and self.get_capabilities(req.model).tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in req.tools]
            if req.tool_choice in ("auto", "none", "required"):
                body["tool_choice"] = req.tool_choice
            elif req.tool_choice:
                body["tool_choice"] = {"type": "function", "function": {"name": req.tool_choice}}
        if req.json_schema and self.get_capabilities(req.model).json_mode and self.spec.id in ("openai", "github_models", "xai", "groq", "openrouter", "together", "mistral", "cerebras", "novita"):
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "schema": req.json_schema, "strict": False}}
        elif (req.json_mode or req.json_schema) and self.get_capabilities(req.model).json_mode:
            body["response_format"] = {"type": "json_object"}
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        body.update(req.extra or {})
        return body

    def _is_reasoning_model(self, model: str) -> bool:
        m = model.lower()
        return self.spec.id == "openai" and (m.startswith("o1") or m.startswith("o3") or m.startswith("o4") or m.startswith("gpt-5"))

    def _uses_max_completion_tokens(self, model: str) -> bool:
        return self.spec.id in ("openai", "github_models") and self._is_reasoning_model(model)

    @staticmethod
    def _msg(m: ChatMessage) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": m.role, "content": m.content}
        if m.name:
            d["name"] = m.name
        if m.role == "tool" and m.tool_call_id:
            d["tool_call_id"] = m.tool_call_id
        if m.role == "assistant" and m.tool_calls:
            d["tool_calls"] = [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc.get("arguments") or {})}}
                for tc in m.tool_calls
            ]
            if not m.content:
                d["content"] = None
        return d

    def chat(self, request: ChatRequest) -> ChatResult:
        data = self._request("POST", self.chat_path, json_body=self._build_body(request))
        return self._parse_chat(data)

    def _parse_chat(self, data: Dict[str, Any]) -> ChatResult:
        choices = data.get("choices") or []
        if not choices:
            raise StructuredOutputError("empty response from provider", provider=self.spec.id)
        msg = choices[0].get("message") or {}
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except ValueError:
                args = {"_raw": args_raw}
            tool_calls.append({"id": tc.get("id") or f"call_{len(tool_calls)}", "name": fn.get("name", ""), "arguments": args})
        content = msg.get("content") or ""
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        usage = data.get("usage") or {}
        return ChatResult(
            content=content,
            tool_calls=tool_calls,
            usage=Usage(input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens")),
            model=data.get("model", ""),
            finish_reason=choices[0].get("finish_reason", "") or "",
            raw=data,
        )

    def stream_chat(self, request: ChatRequest) -> Iterator[str]:
        body = self._build_body(request, stream=True)
        with self.http.stream("POST", f"{self.base_url}{self.chat_path}", json=body, headers=self._headers()) as resp:
            if resp.status_code >= 400:
                resp.read()
                raise self.map_http_error(resp)
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                for ch in chunk.get("choices") or []:
                    delta = (ch.get("delta") or {}).get("content")
                    if delta:
                        yield delta

    # ---- embeddings ----------------------------------------------------
    def embeddings(self, texts: List[str], model: str) -> EmbeddingResult:
        if not self.spec.capabilities.embeddings:
            raise UnsupportedCapabilityError("embeddings not supported", provider=self.spec.id)
        data = self._request("POST", self.embeddings_path, json_body={"model": model, "input": texts})
        vectors = [d.get("embedding", []) for d in data.get("data", [])]
        usage = data.get("usage") or {}
        return EmbeddingResult(vectors=vectors, usage=Usage(input_tokens=usage.get("prompt_tokens")), model=data.get("model", model))

    # ---- images ---------------------------------------------------------
    def image_generation(self, prompt: str, model: str, **kwargs) -> ImageResult:
        if not self.spec.capabilities.image_generation:
            raise UnsupportedCapabilityError("image generation not supported", provider=self.spec.id)
        body = {"model": model, "prompt": prompt, "n": 1}
        body.update({k: v for k, v in kwargs.items() if v is not None})
        data = self._request("POST", self.images_path, json_body=body)
        images, urls = [], []
        import base64

        for item in data.get("data", []):
            if item.get("b64_json"):
                images.append(base64.b64decode(item["b64_json"]))
            elif item.get("url"):
                urls.append(item["url"])
        return ImageResult(images=images, urls=urls, model=model)
