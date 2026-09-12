# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""High-level AI client used by the rest of Flowboard.

    client = AIClient(db, user, board=board)
    result = client.chat(messages)                     # uses effective model
    data   = client.structured(messages, schema=...)   # validated JSON

Responsibilities:
  * resolve the effective `provider:model` (board override -> account default
    -> first enabled provider's default model) - never an operator key
  * fetch + decrypt the user's credential and build the adapter
  * record usage (tokens, latency, cost estimate, error kind)
  * optional, explicitly opt-in fallback to a second model
  * structured generation with graceful degradation:
      tools -> native JSON schema -> JSON mode -> prompt + extraction
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import jsonschema
from sqlalchemy.orm import Session

from ..models import Board, User, UserProfile
from ..services.boards import board_settings
from . import credentials as cred_service
from .base import ProviderAdapter
from .registry import get_spec, parse_model_ref
from .types import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    ModelInfo,
    NoProviderConfiguredError,
    ProviderError,
    StructuredOutputError,
    ToolSpec,
)


class ResolvedModel:
    def __init__(self, provider: str, model: str, source: str):
        self.provider, self.model, self.source = provider, model, source

    @property
    def ref(self) -> str:
        return f"{self.provider}:{self.model}"


def resolve_model(db: Session, user: User, board: Optional[Board] = None, explicit: Optional[str] = None) -> Optional[ResolvedModel]:
    """Inheritance: explicit -> board override -> account default -> first
    enabled provider with a default model.  Only models on providers the user
    has *enabled* credentials for are returned."""
    enabled = {c.provider: c for c in cred_service.list_credentials(db, user) if c.enabled}
    candidates: List[Tuple[Optional[str], str]] = []
    if explicit:
        candidates.append((explicit, "explicit"))
    if board is not None:
        candidates.append((board_settings(board).get("ai_model"), "board"))
    profile: Optional[UserProfile] = user.profile
    if profile is not None:
        candidates.append((profile.default_ai_model, "account"))
    for ref, source in candidates:
        if not ref:
            continue
        try:
            provider, model = parse_model_ref(ref)
        except ValueError:
            continue
        if provider in enabled:
            return ResolvedModel(provider, model, source)
    for provider, cred in enabled.items():
        if cred.default_model:
            return ResolvedModel(provider, cred.default_model, "provider_default")
    return None


def available_models(db: Session, user: User) -> List[Dict[str, Any]]:
    """Grouped model list for pickers: [{provider, provider_name, groups: [{family, models: [...]}]}]."""
    out = []
    for cred in cred_service.list_credentials(db, user):
        if not cred.enabled:
            continue
        spec = get_spec(cred.provider)
        models = cred_service.models_for_credential(db, cred)
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for m in models:
            if "embedding" in m.tags or "audio" in m.tags:
                continue
            groups.setdefault(m.family or spec.family_for(m.id), []).append({**m.to_dict(), "ref": f"{cred.provider}:{m.id}"})
        out.append({
            "provider": cred.provider,
            "provider_name": cred.label or spec.name,
            "default_model": cred.default_model,
            "validation_status": cred.validation_status,
            "groups": [{"family": f, "models": ms} for f, ms in sorted(groups.items(), key=lambda kv: kv[0].lower())],
        })
    return out


class AIClient:
    def __init__(self, db: Session, user: User, board: Optional[Board] = None, model_ref: Optional[str] = None):
        self.db = db
        self.user = user
        self.board = board
        self.resolved = resolve_model(db, user, board, model_ref)

    # ---- plumbing ------------------------------------------------------
    def _adapter(self, provider: str) -> ProviderAdapter:
        cred = cred_service.get_credential(self.db, self.user, provider)
        if cred is None or not cred.enabled:
            raise NoProviderConfiguredError(f"provider {provider} is not configured", provider=provider)
        return cred_service.adapter_for(cred)

    def _fallback(self) -> Optional[ResolvedModel]:
        profile = self.user.profile
        if profile is None or not profile.ai_fallback_enabled or not profile.ai_fallback_model:
            return None
        try:
            p, m = parse_model_ref(profile.ai_fallback_model)
        except ValueError:
            return None
        if self.resolved and p == self.resolved.provider and m == self.resolved.model:
            return None
        cred = cred_service.get_credential(self.db, self.user, p)
        if cred is None or not cred.enabled:
            return None
        return ResolvedModel(p, m, "fallback")

    def _run(self, operation: str, target: ResolvedModel, fn, summary: str = ""):
        from .usage import record_usage

        started = time.perf_counter()
        adapter = self._adapter(target.provider)
        try:
            result = fn(adapter, target.model)
        except ProviderError as exc:
            record_usage(self.db, self.user, provider=target.provider, model=target.model, operation=operation, latency_ms=int((time.perf_counter() - started) * 1000), board_id=self.board.id if self.board else None, success=False, error_kind=exc.kind, error_message=exc.detail, request_summary=summary)
            raise
        usage = getattr(result, "usage", None)
        record_usage(
            self.db, self.user, provider=target.provider, model=target.model, operation=operation,
            latency_ms=int((time.perf_counter() - started) * 1000), board_id=self.board.id if self.board else None,
            input_tokens=getattr(usage, "input_tokens", None), output_tokens=getattr(usage, "output_tokens", None),
            request_summary=summary, response_summary=getattr(result, "content", "") or "",
        )
        return result

    def _with_fallback(self, operation: str, fn, summary: str = ""):
        if self.resolved is None:
            raise NoProviderConfiguredError()
        try:
            return self._run(operation, self.resolved, fn, summary)
        except ProviderError as exc:
            if exc.kind in ("bad_request", "unsupported", "structured_output"):
                raise
            fb = self._fallback()
            if fb is None:
                raise
            return self._run(operation, fb, fn, summary)

    # ---- public API ----------------------------------------------------
    def chat(self, messages: List[ChatMessage], *, tools: Optional[List[ToolSpec]] = None, tool_choice: Optional[str] = None, temperature: Optional[float] = 0.2, max_tokens: int = 1024, operation: str = "chat") -> ChatResult:
        def fn(adapter: ProviderAdapter, model: str) -> ChatResult:
            return adapter.chat(ChatRequest(messages=messages, model=model, tools=tools, tool_choice=tool_choice, temperature=temperature, max_tokens=max_tokens))

        return self._with_fallback(operation, fn, summary=_summarise(messages))

    def stream(self, messages: List[ChatMessage], *, temperature: Optional[float] = 0.2, max_tokens: int = 1024) -> Iterator[str]:
        if self.resolved is None:
            raise NoProviderConfiguredError()
        adapter = self._adapter(self.resolved.provider)
        return adapter.stream_chat(ChatRequest(messages=messages, model=self.resolved.model, temperature=temperature, max_tokens=max_tokens))

    def structured(self, messages: List[ChatMessage], *, schema: Dict[str, Any], name: str = "result", description: str = "Return the result", max_tokens: int = 2048, temperature: float = 0.1, operation: str = "structured") -> Dict[str, Any]:
        """Return a dict validated against `schema` using the best strategy the
        provider supports.  Raises StructuredOutputError if no strategy yields
        valid JSON after one repair attempt."""

        def fn(adapter: ProviderAdapter, model: str):
            caps = adapter.get_capabilities(model)
            attempts: List[str] = []
            if caps.tools:
                attempts.append("tools")
            if caps.json_mode:
                attempts.append("json")
            attempts.append("prompt")
            last_error: Optional[Exception] = None
            for strategy in attempts:
                try:
                    return self._structured_attempt(adapter, model, strategy, messages, schema, name, description, max_tokens, temperature)
                except StructuredOutputError as exc:
                    last_error = exc
                    continue
                except ProviderError as exc:
                    # provider refused the mode (e.g. tools unsupported on this model) -> try next strategy
                    if exc.kind in ("bad_request", "unsupported"):
                        last_error = exc
                        continue
                    raise
            raise StructuredOutputError(str(last_error) if last_error else "no strategy succeeded", provider=adapter.spec.id)

        return self._with_fallback(operation, fn, summary=_summarise(messages)).data

    def _structured_attempt(self, adapter, model, strategy, messages, schema, name, description, max_tokens, temperature):
        if strategy == "tools":
            req = ChatRequest(messages=messages, model=model, tools=[ToolSpec(name=name, description=description, parameters=schema)], tool_choice=name, temperature=temperature, max_tokens=max_tokens)
            res = adapter.chat(req)
            for tc in res.tool_calls:
                if tc.get("name") == name and isinstance(tc.get("arguments"), dict):
                    data = tc["arguments"]
                    break
            else:
                data = _extract_json(res.content)
        else:
            hint = ChatMessage(role="system", content=f"Respond with ONLY a single JSON object (no prose, no code fences) matching this JSON schema:\n{json.dumps(schema)}")
            req = ChatRequest(messages=[hint] + list(messages), model=model, temperature=temperature, max_tokens=max_tokens, json_schema=schema if strategy == "json" else None, json_mode=strategy == "json")
            res = adapter.chat(req)
            data = _extract_json(res.content)
        if data is None:
            raise StructuredOutputError("no JSON object in response", provider=adapter.spec.id)
        try:
            jsonschema.validate(data, schema)
        except jsonschema.ValidationError as exc:
            # one repair round-trip
            repair = list(messages) + [
                ChatMessage(role="assistant", content=json.dumps(data)),
                ChatMessage(role="user", content=f"That JSON was invalid: {exc.message}. Return corrected JSON only."),
            ]
            res2 = adapter.chat(ChatRequest(messages=[ChatMessage(role="system", content=f"Respond with ONLY JSON matching: {json.dumps(schema)}")] + repair, model=model, temperature=0, max_tokens=max_tokens, json_mode=strategy == "json"))
            data = _extract_json(res2.content)
            try:
                jsonschema.validate(data, schema)
            except (jsonschema.ValidationError, TypeError) as exc2:
                raise StructuredOutputError(f"schema validation failed: {getattr(exc2, 'message', exc2)}", provider=adapter.spec.id)
        # attach usage for the recorder
        class _R:  # minimal object with .usage/.content for _run
            pass

        r = _R()
        r.usage = res.usage
        r.content = json.dumps(data)[:2000]
        r.data = data
        return r

    # ---- introspection ------------------------------------------------
    @property
    def model_ref(self) -> Optional[str]:
        return self.resolved.ref if self.resolved else None


def _summarise(messages: List[ChatMessage]) -> str:
    last = next((m.content for m in reversed(messages) if m.role == "user"), "")
    return (last or "")[:500]


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE | re.DOTALL)
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    m = re.search(r"\{.*\}", s, flags=re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None
