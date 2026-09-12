# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Provider registry.

`PROVIDERS` is the single place that declares which providers exist, how they
authenticate, and which adapter class speaks their protocol.  Adding a
provider that is OpenAI-compatible is a ~10 line `ProviderSpec` entry; a
provider with its own protocol also needs an adapter module (see
docs/AI_PROVIDERS.md).

Model references everywhere else in Flowboard are strings of the form
``provider:model`` (e.g. ``openai:gpt-5``, ``anthropic:claude-sonnet-4-5``).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import ProviderAdapter, ProviderSpec
from .providers import ADAPTERS
from .types import Capabilities, CredentialField, ModelInfo

CATALOG_PATH = Path(__file__).with_name("catalog.json")


def _load_catalog() -> Dict[str, Any]:
    try:
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"fallback_models": {}, "pricing": {}}


CATALOG = _load_catalog()


def _fallback(provider_id: str) -> List[ModelInfo]:
    return [ModelInfo.from_dict(m) for m in CATALOG.get("fallback_models", {}).get(provider_id, [])]


API_KEY = CredentialField("api_key", "API key", secret=True, required=True, placeholder="paste your key")
BASE_URL = CredentialField("base_url", "Base URL (optional)", secret=False, required=False, placeholder="override only for proxies / regional endpoints")

FULL = Capabilities(chat=True, streaming=True, tools=True, json_mode=True, vision=True, reasoning=True, embeddings=True, image_generation=True, list_models=True)


def caps(**kw) -> Capabilities:
    base = dict(chat=True, streaming=True, tools=False, json_mode=False, vision=False, reasoning=False, embeddings=False, image_generation=False, list_models=True)
    base.update(kw)
    return Capabilities(**base)


PROVIDERS: Dict[str, ProviderSpec] = {}


def register(spec: ProviderSpec) -> ProviderSpec:
    if not spec.fallback_models:
        spec.fallback_models = _fallback(spec.id)
    PROVIDERS[spec.id] = spec
    return spec


register(ProviderSpec(
    id="openai", name="OpenAI", docs_url="https://platform.openai.com/docs", console_url="https://platform.openai.com/api-keys",
    credential_fields=[API_KEY], config_fields=[CredentialField("organization", "Organization ID (optional)", secret=False, required=False), CredentialField("project", "Project ID (optional)", secret=False, required=False), BASE_URL],
    default_base_url="https://api.openai.com/v1",
    capabilities=caps(tools=True, json_mode=True, vision=True, reasoning=True, embeddings=True, image_generation=True),
    family_rules=[(r"^o[1-9]", "o-series (reasoning)"), (r"^gpt-5", "GPT-5"), (r"^gpt-4", "GPT-4"), (r"^gpt-3", "GPT-3.5"), (r"embedding", "Embeddings"), (r"image|dall-e", "Images"), (r"whisper|tts|audio|realtime|transcribe", "Audio")],
    hide_model_patterns=[r"davinci|babbage|ft:|moderation|search|similarity|edit|codex-mini|computer-use|-\d{4}-\d{2}-\d{2}$"],
))
register(ProviderSpec(
    id="anthropic", name="Anthropic", docs_url="https://docs.anthropic.com", console_url="https://console.anthropic.com/settings/keys",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://api.anthropic.com/v1", adapter="anthropic",
    capabilities=caps(tools=True, json_mode=False, vision=True, reasoning=True),
    family_rules=[(r"opus", "Claude Opus"), (r"sonnet", "Claude Sonnet"), (r"haiku", "Claude Haiku")],
    notes="Structured output is obtained through tool calling.",
))
register(ProviderSpec(
    id="xai", name="xAI (Grok)", docs_url="https://docs.x.ai", console_url="https://console.x.ai",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://api.x.ai/v1",
    capabilities=caps(tools=True, json_mode=True, vision=True, reasoning=True, image_generation=True),
    family_rules=[(r"grok-4", "Grok 4"), (r"grok-3", "Grok 3"), (r"grok-2", "Grok 2"), (r"image", "Images")],
))
register(ProviderSpec(
    id="groq", name="Groq", docs_url="https://console.groq.com/docs", console_url="https://console.groq.com/keys",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://api.groq.com/openai/v1",
    capabilities=caps(tools=True, json_mode=True, vision=True, reasoning=True),
    family_rules=[(r"llama", "Llama"), (r"qwen", "Qwen"), (r"deepseek", "DeepSeek"), (r"gemma", "Gemma"), (r"mixtral|mistral", "Mistral"), (r"gpt-oss|openai", "OpenAI OSS"), (r"whisper|tts|orpheus", "Audio"), (r"guard|prompt-guard", "Safety")],
    hide_model_patterns=[r"whisper|tts|guard|allam|compound"],
))
register(ProviderSpec(
    id="github_models", name="GitHub Models", docs_url="https://docs.github.com/en/github-models", console_url="https://github.com/settings/personal-access-tokens",
    credential_fields=[CredentialField("api_key", "GitHub personal access token", secret=True, required=True, placeholder="github_pat_… (needs `models:read`)")],
    config_fields=[BASE_URL], default_base_url="https://models.github.ai/inference", models_url="https://models.github.ai/catalog/models",
    capabilities=caps(tools=True, json_mode=True, vision=True, reasoning=True, embeddings=True),
    family_rules=[(r"^openai/", "OpenAI"), (r"^meta/", "Meta"), (r"^mistral", "Mistral"), (r"^microsoft/", "Microsoft"), (r"^deepseek/", "DeepSeek"), (r"^xai/", "xAI"), (r"^cohere/", "Cohere"), (r"^ai21", "AI21")],
    notes="Free/rate-limited playground tier for prototyping; GitHub Copilot subscriptions do not expose an API key, so Copilot itself is not integrable.",
))
register(ProviderSpec(
    id="together", name="Together AI", docs_url="https://docs.together.ai", console_url="https://api.together.xyz/settings/api-keys",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://api.together.xyz/v1",
    capabilities=caps(tools=True, json_mode=True, vision=True, reasoning=True, embeddings=True, image_generation=True),
    family_rules=[(r"llama", "Llama"), (r"qwen", "Qwen"), (r"deepseek", "DeepSeek"), (r"mistral|mixtral", "Mistral"), (r"flux|stable-diffusion", "Images"), (r"embed|bge|m2-bert", "Embeddings"), (r"gemma", "Gemma")],
))
register(ProviderSpec(
    id="mistral", name="Mistral AI", docs_url="https://docs.mistral.ai", console_url="https://console.mistral.ai/api-keys",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://api.mistral.ai/v1",
    capabilities=caps(tools=True, json_mode=True, vision=True, reasoning=True, embeddings=True),
    family_rules=[(r"large", "Mistral Large"), (r"medium", "Mistral Medium"), (r"small|ministral|tiny", "Mistral Small"), (r"codestral|devstral", "Code"), (r"pixtral", "Pixtral (vision)"), (r"magistral", "Magistral (reasoning)"), (r"embed", "Embeddings"), (r"ocr|moderation", "Other")],
))
register(ProviderSpec(
    id="openrouter", name="OpenRouter", docs_url="https://openrouter.ai/docs", console_url="https://openrouter.ai/settings/keys",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://openrouter.ai/api/v1",
    capabilities=caps(tools=True, json_mode=True, vision=True, reasoning=True, embeddings=False, image_generation=False),
    family_rules=[(r"^openai/", "OpenAI"), (r"^anthropic/", "Anthropic"), (r"^google/", "Google"), (r"^meta-llama/", "Meta"), (r"^mistralai/", "Mistral"), (r"^deepseek/", "DeepSeek"), (r"^x-ai/", "xAI"), (r"^qwen/", "Qwen"), (r"^cohere/", "Cohere"), (r"^perplexity/", "Perplexity"), (r"^moonshotai/", "Moonshot")],
    notes="Aggregator: one key reaches many vendors. Model list is large.",
))
register(ProviderSpec(
    id="deepseek", name="DeepSeek", docs_url="https://api-docs.deepseek.com", console_url="https://platform.deepseek.com/api_keys",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://api.deepseek.com/v1",
    capabilities=caps(tools=True, json_mode=True, reasoning=True),
    family_rules=[(r"reasoner", "Reasoning"), (r"chat", "Chat")],
))
register(ProviderSpec(
    id="moonshot", name="Moonshot AI (Kimi)", docs_url="https://platform.moonshot.ai/docs", console_url="https://platform.moonshot.ai/console/api-keys",
    credential_fields=[API_KEY], config_fields=[CredentialField("base_url", "Base URL", secret=False, required=False, placeholder="https://api.moonshot.ai/v1 (or https://api.moonshot.cn/v1)")],
    default_base_url="https://api.moonshot.ai/v1",
    capabilities=caps(tools=True, json_mode=True, vision=True, reasoning=True),
    family_rules=[(r"kimi-k2", "Kimi K2"), (r"kimi", "Kimi"), (r"moonshot-v1", "Moonshot v1")],
))
register(ProviderSpec(
    id="cerebras", name="Cerebras", docs_url="https://inference-docs.cerebras.ai", console_url="https://cloud.cerebras.ai",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://api.cerebras.ai/v1",
    capabilities=caps(tools=True, json_mode=True, reasoning=True),
    family_rules=[(r"llama", "Llama"), (r"qwen", "Qwen"), (r"gpt-oss", "OpenAI OSS")],
))
register(ProviderSpec(
    id="gemini", name="Google Gemini", docs_url="https://ai.google.dev/gemini-api/docs", console_url="https://aistudio.google.com/apikey",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://generativelanguage.googleapis.com/v1beta", adapter="gemini",
    capabilities=caps(tools=True, json_mode=True, vision=True, reasoning=True, embeddings=True, image_generation=True),
    family_rules=[(r"gemini-2\.5", "Gemini 2.5"), (r"gemini-2\.0", "Gemini 2.0"), (r"gemini-1\.5", "Gemini 1.5"), (r"gemma", "Gemma"), (r"embedding", "Embeddings"), (r"imagen|image", "Images"), (r"veo|tts|audio", "Media")],
    hide_model_patterns=[r"aqa|learnlm|-exp-|bison|gecko"],
))
register(ProviderSpec(
    id="perplexity", name="Perplexity", docs_url="https://docs.perplexity.ai", console_url="https://www.perplexity.ai/settings/api",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://api.perplexity.ai",
    capabilities=caps(tools=False, json_mode=True, reasoning=True, list_models=False),
    family_rules=[(r"reasoning|deep-research", "Sonar (reasoning)"), (r"sonar", "Sonar")],
    notes="No model-listing endpoint; models come from catalog.json. Answers include web search.",
))
register(ProviderSpec(
    id="cohere", name="Cohere", docs_url="https://docs.cohere.com", console_url="https://dashboard.cohere.com/api-keys",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://api.cohere.com", adapter="cohere",
    capabilities=caps(tools=True, json_mode=True, embeddings=True),
    family_rules=[(r"command-a", "Command A"), (r"command-r", "Command R"), (r"command", "Command"), (r"embed", "Embeddings"), (r"rerank", "Rerank")],
    hide_model_patterns=[r"rerank|nightly"],
))
register(ProviderSpec(
    id="stability", name="Stability AI", docs_url="https://platform.stability.ai/docs", console_url="https://platform.stability.ai/account/keys",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://api.stability.ai", adapter="stability",
    capabilities=Capabilities(chat=False, streaming=False, tools=False, json_mode=False, vision=False, reasoning=False, embeddings=False, image_generation=True, list_models=False),
    family_rules=[(r"sd3", "Stable Diffusion 3.5"), (r"core|ultra", "Stable Image")],
    notes="Image generation only.",
))
register(ProviderSpec(
    id="novita", name="Novita AI", docs_url="https://novita.ai/docs", console_url="https://novita.ai/settings/key-management",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://api.novita.ai/v3/openai",
    capabilities=caps(tools=True, json_mode=True, vision=True, reasoning=True),
    family_rules=[(r"deepseek", "DeepSeek"), (r"llama", "Llama"), (r"qwen", "Qwen"), (r"mistral", "Mistral"), (r"gemma", "Gemma")],
))
register(ProviderSpec(
    id="minimax", name="MiniMax", docs_url="https://www.minimax.io/platform/document", console_url="https://www.minimax.io/platform/user-center/basic-information/interface-key",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://api.minimax.io/v1",
    capabilities=caps(tools=True, json_mode=False, reasoning=True, list_models=False),
    family_rules=[(r"m1|m2", "MiniMax M-series (reasoning)"), (r"text", "MiniMax Text")],
    notes="No model-listing endpoint; models come from catalog.json.",
))
register(ProviderSpec(
    id="replicate", name="Replicate", docs_url="https://replicate.com/docs", console_url="https://replicate.com/account/api-tokens",
    credential_fields=[CredentialField("api_key", "API token", secret=True, required=True, placeholder="r8_…")],
    config_fields=[CredentialField("models", "Extra models (comma separated owner/name)", secret=False, required=False, placeholder="owner/model, owner/model:version")],
    default_base_url="https://api.replicate.com", adapter="replicate",
    capabilities=Capabilities(chat=True, streaming=False, tools=False, json_mode=False, vision=False, reasoning=False, embeddings=False, image_generation=True, list_models=False),
    family_rules=[(r"flux|sdxl|stable-diffusion|imagen|ideogram|recraft", "Images"), (r"llama|mistral|qwen|deepseek", "Language")],
    notes="Runs arbitrary models via predictions; curated list + user-pinned models.", status="limited",
))
register(ProviderSpec(
    id="octoai", name="OctoAI (discontinued)", docs_url="https://octo.ai", console_url="",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://text.octoai.run/v1",
    capabilities=caps(tools=True, json_mode=True, list_models=False),
    notes="OctoAI's public inference service shut down in late 2024 after the NVIDIA acquisition. Kept for users with a private/enterprise endpoint (set Base URL).", status="legacy",
))
register(ProviderSpec(
    id="azure_openai", name="Azure OpenAI", docs_url="https://learn.microsoft.com/azure/ai-services/openai/", console_url="https://portal.azure.com",
    credential_fields=[CredentialField("api_key", "Azure OpenAI key", secret=True, required=True, placeholder="from the resource's Keys and Endpoint page")],
    config_fields=[CredentialField("base_url", "Resource endpoint", secret=False, required=True, placeholder="https://my-resource.openai.azure.com"), CredentialField("deployments", "Deployment names (comma separated)", secret=False, required=True, placeholder="gpt-4o, gpt-4o-mini")],
    default_base_url="", adapter="azure_openai",
    capabilities=caps(tools=True, json_mode=True, vision=True, reasoning=True, embeddings=True),
    family_rules=[(r"o[1-9]", "o-series (reasoning)"), (r"gpt-5", "GPT-5"), (r"gpt-4", "GPT-4"), (r"embedding", "Embeddings")],
    notes="Models are your deployment names. Uses the /openai/v1 compatibility endpoint.",
))
register(ProviderSpec(
    id="huggingface", name="Hugging Face Inference", docs_url="https://huggingface.co/docs/inference-providers", console_url="https://huggingface.co/settings/tokens",
    credential_fields=[CredentialField("api_key", "Access token", secret=True, required=True, placeholder="hf_…")],
    config_fields=[BASE_URL, CredentialField("models", "Models to pin (comma separated)", secret=False, required=False, placeholder="meta-llama/Llama-3.3-70B-Instruct, Qwen/Qwen2.5-72B-Instruct")],
    default_base_url="https://router.huggingface.co/v1",
    capabilities=caps(tools=True, json_mode=True, vision=True, reasoning=True, list_models=True),
    family_rules=[(r"llama", "Llama"), (r"qwen", "Qwen"), (r"deepseek", "DeepSeek"), (r"mistral|mixtral", "Mistral"), (r"gemma", "Gemma"), (r"phi", "Phi")],
    notes="Routes to HF Inference Providers (Cerebras, Groq, Together…) with one token.",
))
register(ProviderSpec(
    id="fireworks", name="Fireworks AI", docs_url="https://docs.fireworks.ai", console_url="https://fireworks.ai/account/api-keys",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://api.fireworks.ai/inference/v1",
    capabilities=caps(tools=True, json_mode=True, vision=True, reasoning=True, embeddings=True, image_generation=True),
    family_rules=[(r"llama", "Llama"), (r"qwen", "Qwen"), (r"deepseek", "DeepSeek"), (r"mistral|mixtral", "Mistral"), (r"kimi", "Kimi"), (r"gpt-oss", "OpenAI OSS"), (r"flux|stable", "Images")],
))
register(ProviderSpec(
    id="deepinfra", name="DeepInfra", docs_url="https://deepinfra.com/docs", console_url="https://deepinfra.com/dash/api_keys",
    credential_fields=[API_KEY], config_fields=[BASE_URL], default_base_url="https://api.deepinfra.com/v1/openai",
    capabilities=caps(tools=True, json_mode=True, vision=True, reasoning=True, embeddings=True),
    family_rules=[(r"llama", "Llama"), (r"qwen", "Qwen"), (r"deepseek", "DeepSeek"), (r"mistral|mixtral", "Mistral"), (r"gemma", "Gemma"), (r"embed|bge", "Embeddings")],
))
register(ProviderSpec(
    id="ollama", name="Ollama (local)", docs_url="https://github.com/ollama/ollama/blob/main/docs/openai.md", console_url="https://ollama.com/download",
    credential_fields=[CredentialField("api_key", "API key", secret=True, required=False, placeholder="not needed for a local server (or your ollama.com key)")],
    config_fields=[CredentialField("base_url", "Base URL", secret=False, required=True, placeholder="http://127.0.0.1:11434/v1")],
    default_base_url="http://127.0.0.1:11434/v1",
    capabilities=caps(tools=True, json_mode=True, vision=True, embeddings=True),
    family_rules=[(r"llama", "Llama"), (r"qwen", "Qwen"), (r"deepseek", "DeepSeek"), (r"mistral|mixtral", "Mistral"), (r"gemma", "Gemma"), (r"phi", "Phi"), (r"embed", "Embeddings")],
    notes="Runs on your own machine; Flowboard's server must be able to reach the URL (use a tunnel or Tailscale for a remote Ollama).",
))
register(ProviderSpec(
    id="lmstudio", name="LM Studio (local)", docs_url="https://lmstudio.ai/docs/app/api/endpoints/openai", console_url="https://lmstudio.ai",
    credential_fields=[CredentialField("api_key", "API key", secret=True, required=False, placeholder="optional")],
    config_fields=[CredentialField("base_url", "Base URL", secret=False, required=True, placeholder="http://127.0.0.1:1234/v1")],
    default_base_url="http://127.0.0.1:1234/v1",
    capabilities=caps(tools=True, json_mode=True, embeddings=True),
    notes="Start the LM Studio local server first (Developer tab).",
))
register(ProviderSpec(
    id="vllm", name="vLLM / self-hosted", docs_url="https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html", console_url="",
    credential_fields=[CredentialField("api_key", "API key", secret=True, required=False, placeholder="whatever you started the server with (--api-key)")],
    config_fields=[CredentialField("base_url", "Base URL", secret=False, required=True, placeholder="http://gpu-box:8000/v1")],
    default_base_url="",
    capabilities=caps(tools=True, json_mode=True, embeddings=True),
    notes="Also works for llama.cpp server, TGI, SGLang and LocalAI.",
))
register(ProviderSpec(
    id="custom_openai", name="Custom OpenAI-compatible", docs_url="", console_url="",
    credential_fields=[CredentialField("api_key", "API key", secret=True, required=False, placeholder="leave blank for local servers (Ollama, LM Studio, vLLM)")],
    config_fields=[CredentialField("base_url", "Base URL", secret=False, required=True, placeholder="http://127.0.0.1:11434/v1"), CredentialField("label", "Display name", secret=False, required=False, placeholder="My local Ollama")],
    default_base_url="",
    capabilities=caps(tools=True, json_mode=True),
    notes="Any server that speaks the OpenAI Chat Completions API.",
))


# --- lookup helpers -----------------------------------------------------

def get_spec(provider_id: str) -> ProviderSpec:
    try:
        return PROVIDERS[provider_id]
    except KeyError:
        raise KeyError(f"unknown AI provider '{provider_id}'")


def list_specs(include_legacy: bool = True) -> List[ProviderSpec]:
    return [s for s in PROVIDERS.values() if include_legacy or s.status != "legacy"]


def build_adapter(provider_id: str, secrets: Dict[str, str], config: Dict[str, Any]) -> ProviderAdapter:
    spec = get_spec(provider_id)
    cls = ADAPTERS[spec.adapter]
    return cls(spec, secrets, config)


def parse_model_ref(ref: str) -> Tuple[str, str]:
    """'openai:gpt-5' -> ('openai', 'gpt-5').  Model ids may themselves contain
    ':' (Replicate versions) so split on the first one only."""
    if not ref or ":" not in ref:
        raise ValueError(f"invalid model reference '{ref}' (expected provider:model)")
    provider, model = ref.split(":", 1)
    if provider not in PROVIDERS or not model:
        raise ValueError(f"invalid model reference '{ref}'")
    return provider, model


def make_model_ref(provider: str, model: str) -> str:
    return f"{provider}:{model}"


def pricing_for(provider: str, model: str) -> Optional[Tuple[float, float]]:
    """Per-1M-token (input, output) USD pricing from catalog.json, matched by
    exact id first and then by longest prefix (so dated snapshots inherit)."""
    table = CATALOG.get("pricing", {}).get(provider, {})
    if model in table:
        return tuple(table[model])
    best: Optional[str] = None
    for key in table:
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return tuple(table[best]) if best else None


def estimate_cost(provider: str, model: str, input_tokens: Optional[int], output_tokens: Optional[int]) -> Optional[float]:
    price = pricing_for(provider, model)
    if price is None:
        return None
    inp, out = price
    return round(((input_tokens or 0) / 1e6) * inp + ((output_tokens or 0) / 1e6) * out, 6)
