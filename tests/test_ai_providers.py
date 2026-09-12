# SPDX-License-Identifier: AGPL-3.0-or-later
"""Credential storage, provider registry/dispatch, structured output fallbacks."""
import json

import httpx
import pytest

from app.ai import credentials as cred_service
from app.ai.base import ProviderAdapter
from app.ai.client import AIClient, resolve_model, available_models
from app.ai.providers.openai_compat import OpenAICompatAdapter
from app.ai.registry import PROVIDERS, build_adapter, estimate_cost, get_spec, parse_model_ref
from app.ai.types import AuthenticationError, ChatMessage, ChatRequest, InsufficientCreditsError, RateLimitedError, StructuredOutputError, NoProviderConfiguredError
from app.models import AIProviderCredential
from app.services import boards as board_service


def test_registry_covers_requested_providers():
    for pid in ["openai", "anthropic", "xai", "groq", "github_models", "together", "mistral", "openrouter", "deepseek", "moonshot", "cerebras", "gemini", "perplexity", "cohere", "stability", "novita", "minimax", "replicate", "octoai"]:
        assert pid in PROVIDERS, pid
    assert parse_model_ref("openai:gpt-5") == ("openai", "gpt-5")
    assert parse_model_ref("replicate:owner/model:abc123") == ("replicate", "owner/model:abc123")
    with pytest.raises(ValueError):
        parse_model_ref("nope:model")
    assert estimate_cost("openai", "gpt-4o-mini", 1_000_000, 1_000_000) == pytest.approx(0.75)
    assert estimate_cost("openai", "gpt-4o-mini-2024-07-18", 1_000_000, 0) == pytest.approx(0.15)  # prefix match
    assert estimate_cost("custom_openai", "whatever", 10, 10) is None


def test_credentials_encrypted_and_scoped(db, alice, bob):
    cred = cred_service.upsert_credential(db, alice, "openai", secrets={"api_key": "sk-proj-abcdefghijklmnopqrstuvwxyz1234"}, config={"organization": "org-1"})
    assert b"sk-proj" not in cred.secret_blob
    assert cred.secret_hint == "sk-proj-••••••••1234"
    assert json.loads(cred.config_json)["organization"] == "org-1"
    assert cred_service.decrypt_secrets(cred)["api_key"].startswith("sk-proj")
    # other user cannot see or fetch it
    assert cred_service.list_credentials(db, bob) == []
    assert cred_service.get_credential_by_id(db, bob, cred.id) is None
    # blank re-submit keeps the key; config edits work
    cred2 = cred_service.upsert_credential(db, alice, "openai", secrets={"api_key": ""}, config={"organization": "org-2"})
    assert cred_service.decrypt_secrets(cred2)["api_key"].endswith("1234")
    assert json.loads(cred2.config_json)["organization"] == "org-2"
    # replacement without revealing the original
    cred3 = cred_service.upsert_credential(db, alice, "openai", secrets={"api_key": "sk-new-key-9999"}, config={})
    assert cred_service.decrypt_secrets(cred3)["api_key"] == "sk-new-key-9999"
    with pytest.raises(ValueError):
        cred_service.upsert_credential(db, bob, "openai", secrets={"api_key": ""}, config={})


def test_required_config_enforced(db, alice):
    with pytest.raises(ValueError):
        cred_service.upsert_credential(db, alice, "custom_openai", secrets={"api_key": ""}, config={"base_url": ""})
    c = cred_service.upsert_credential(db, alice, "custom_openai", secrets={"api_key": ""}, config={"base_url": "http://127.0.0.1:11434/v1"})
    assert cred_service.adapter_for(c).base_url == "http://127.0.0.1:11434/v1"


def _mock_adapter(handler):
    spec = get_spec("openai")
    transport = httpx.MockTransport(handler)
    return OpenAICompatAdapter(spec, {"api_key": "sk-test"}, {}, http=httpx.Client(transport=transport))


def test_openai_compat_dispatch_and_error_mapping():
    def handler(request: httpx.Request):
        assert request.headers["authorization"] == "Bearer sk-test"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}, {"id": "o3"}, {"id": "text-embedding-3-small"}, {"id": "gpt-4o-mini-2024-07-18"}]})
        body = json.loads(request.content)
        if body["model"] == "limited":
            return httpx.Response(429, json={"error": {"message": "Rate limit reached"}}, headers={"retry-after": "3"})
        if body["model"] == "broke":
            return httpx.Response(429, json={"error": {"message": "You exceeded your current quota, please check your plan and billing"}})
        if body["model"] == "badkey":
            return httpx.Response(401, json={"error": {"message": "Incorrect API key"}})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}, "model": body["model"]})

    a = _mock_adapter(handler)
    models = a.list_models()
    ids = [m.id for m in models]
    assert "gpt-4o-mini" in ids and "o3" in ids and "gpt-4o-mini-2024-07-18" not in ids  # dated snapshot hidden
    assert next(m for m in models if m.id == "o3").family == "o-series (reasoning)"
    assert "reasoning" in next(m for m in models if m.id == "o3").tags
    assert "embedding" in next(m for m in models if m.id == "text-embedding-3-small").tags
    res = a.chat(ChatRequest(messages=[ChatMessage("user", "hello")], model="gpt-4o-mini"))
    assert res.content == "hi" and res.usage.input_tokens == 5
    with pytest.raises(RateLimitedError) as e:
        a.chat(ChatRequest(messages=[ChatMessage("user", "x")], model="limited"))
    assert e.value.retry_after == 3.0
    with pytest.raises(InsufficientCreditsError):
        a.chat(ChatRequest(messages=[ChatMessage("user", "x")], model="broke"))
    with pytest.raises(AuthenticationError):
        a.chat(ChatRequest(messages=[ChatMessage("user", "x")], model="badkey"))


def test_reasoning_models_use_max_completion_tokens():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    a = _mock_adapter(handler)
    a.chat(ChatRequest(messages=[ChatMessage("user", "x")], model="o3", max_tokens=50, temperature=0.2))
    assert seen["max_completion_tokens"] == 50 and "temperature" not in seen
    a.chat(ChatRequest(messages=[ChatMessage("user", "x")], model="gpt-4o-mini", max_tokens=50))
    assert seen["max_tokens"] == 50


class ToolLessAdapter(ProviderAdapter):
    """Simulates a provider without tool calling or JSON mode: prompt-only."""

    calls = 0

    def chat(self, request):
        ToolLessAdapter.calls += 1
        from app.ai.types import ChatResult, Usage

        assert request.tools is None
        if ToolLessAdapter.calls == 1:
            return ChatResult(content='Sure! ```json\n{"message": "hi", "operations": "oops"}\n```', usage=Usage(1, 1))
        return ChatResult(content='{"message": "hi", "operations": []}', usage=Usage(1, 1))


def test_structured_fallback_prompt_only_with_repair(db, alice, monkeypatch):
    from app.ai import registry, providers
    from app.ai.types import Capabilities

    spec = registry.ProviderSpec(id="perplexity", name="P", adapter="toolless", capabilities=Capabilities(tools=False, json_mode=False, list_models=False), fallback_models=[])
    monkeypatch.setitem(registry.PROVIDERS, "perplexity", spec)
    monkeypatch.setitem(providers.ADAPTERS, "toolless", ToolLessAdapter)
    monkeypatch.setattr(cred_service, "build_adapter", registry.build_adapter)
    cred = cred_service.upsert_credential(db, alice, "perplexity", secrets={"api_key": "pplx-1"}, config={})
    cred.default_model = "sonar"
    ToolLessAdapter.calls = 0
    client = AIClient(db, alice)
    assert client.model_ref == "perplexity:sonar"
    data = client.structured([ChatMessage("user", "x")], schema={"type": "object", "properties": {"message": {"type": "string"}, "operations": {"type": "array"}}, "required": ["message", "operations"]})
    assert data == {"message": "hi", "operations": []}
    assert ToolLessAdapter.calls == 2  # one repair round-trip
    from app.ai.usage import recent_usage

    rec = recent_usage(db, alice)[0]
    assert rec.provider == "perplexity" and rec.operation == "structured" and rec.success


def test_resolve_model_inheritance(db, alice):
    assert resolve_model(db, alice) is None
    with pytest.raises(NoProviderConfiguredError):
        AIClient(db, alice).chat([ChatMessage("user", "x")])
    c1 = cred_service.upsert_credential(db, alice, "groq", secrets={"api_key": "gsk_1"}, config={})
    c1.default_model = "llama-3.1-8b-instant"
    c2 = cred_service.upsert_credential(db, alice, "anthropic", secrets={"api_key": "sk-ant-1"}, config={})
    c2.default_model = "claude-3-5-haiku-latest"
    db.flush()
    assert resolve_model(db, alice).source == "provider_default"
    alice.profile.default_ai_model = "anthropic:claude-3-5-haiku-latest"
    assert resolve_model(db, alice).ref == "anthropic:claude-3-5-haiku-latest"
    board = board_service.list_boards_for_user(db, alice)[0]
    board_service.update_board(db, board, settings={"ai_model": "groq:llama-3.1-8b-instant"})
    r = resolve_model(db, alice, board)
    assert r.ref == "groq:llama-3.1-8b-instant" and r.source == "board"
    # disabled provider is skipped
    c1.enabled = False
    db.flush()
    assert resolve_model(db, alice, board).provider == "anthropic"
    # explicit unknown/foreign provider is ignored
    assert resolve_model(db, alice, board, explicit="openai:gpt-5").provider == "anthropic"
    groups = available_models(db, alice)
    assert [g["provider"] for g in groups] == ["anthropic"]
