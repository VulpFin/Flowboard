# AI provider architecture

Flowboard never talks to an AI vendor with an operator-owned key. Every call is
made server-side with credentials the *user* stored, through a small adapter
layer that hides vendor protocols behind one interface.

```
app/ai/
├── types.py        dataclasses + error hierarchy (ProviderError → AuthenticationError, RateLimitedError, …)
├── base.py         ProviderSpec (metadata, credential schema) + ProviderAdapter (interface, HTTP helpers, error mapping)
├── registry.py     PROVIDERS = {id: ProviderSpec}, model refs "provider:model", pricing helpers
├── catalog.json    fallback model lists + pricing table (maintained by hand, no code changes needed)
├── providers/      adapter implementations (openai_compat, anthropic, gemini, cohere, stability, replicate)
├── credentials.py  encrypted storage + `adapter_for(credential)`
├── client.py       AIClient: model resolution, usage recording, fallback, structured output
├── usage.py        AIUsageRecord writer/summaries
├── assistant.py    board assistant → AIChangeSet proposals
└── enrich.py       task tagging/sizing
```

## Model references

Everything outside `app/ai` refers to models as `provider:model`, e.g.
`openai:gpt-5`, `anthropic:claude-sonnet-4-5`, `openrouter:google/gemini-2.5-flash`,
`replicate:black-forest-labs/flux-schnell`. `registry.parse_model_ref` splits on
the first colon (Replicate ids may contain more).

Resolution order (`client.resolve_model`): explicit request → board setting
`ai_model` → account default (`UserProfile.default_ai_model`) → the first
enabled provider's default model. A model is only ever chosen if the user has an
*enabled* credential for its provider.

Fallback (`UserProfile.ai_fallback_*`) is off by default and only kicks in for
transient/provider errors (rate limit, outage, credits, auth) — never for bad
requests — so it cannot silently spend money unless the user opted in.

## The adapter interface

```python
class ProviderAdapter:
    def get_capabilities(model=None) -> Capabilities
    def validate_credentials() -> str          # human-readable OK message or raise ProviderError
    def list_models() -> list[ModelInfo]
    def chat(ChatRequest) -> ChatResult        # tool calls normalised to [{id, name, arguments}]
    def stream_chat(ChatRequest) -> Iterator[str]
    def embeddings(texts, model) -> EmbeddingResult   # optional
    def image_generation(prompt, model, **kw) -> ImageResult  # optional
```

Unsupported operations raise `UnsupportedCapabilityError` from the base class;
adapters only override what the vendor supports. `Capabilities` (chat, streaming,
tools, json_mode, vision, reasoning, embeddings, image_generation, list_models)
is declared per provider in the spec and can be refined per model.

Structured output (`AIClient.structured`) degrades gracefully:
1. tool calling with the schema as the single forced tool,
2. native JSON schema / JSON mode,
3. plain prompting + JSON extraction,
each validated with `jsonschema`, with one automatic repair round-trip. This is
how providers with weak tool calling still drive the assistant.

## Provider status

| Provider | Adapter | Notes |
|---|---|---|
| OpenAI | openai_compat | full; o-series/GPT-5 use `max_completion_tokens`, no temperature |
| Anthropic | anthropic (native Messages API) | structured output via tools |
| xAI, Groq, Together, Mistral, OpenRouter, DeepSeek, Moonshot/Kimi, Cerebras, Novita, MiniMax, GitHub Models | openai_compat | shared adapter; MiniMax & Perplexity have no model-list endpoint → catalog fallback |
| Google Gemini | gemini (native) | chat, tools, JSON schema, embeddings, image output |
| Perplexity | openai_compat | no tools; JSON mode; catalog model list |
| Cohere | cohere (native v2) | chat, tools, embeddings |
| Stability AI | stability | image generation only; validation via balance endpoint |
| Replicate | replicate | predictions API; curated + user-pinned models; chat via `prompt` input |
| OctoAI | openai_compat (legacy) | public service discontinued in 2024; kept for private endpoints |
| Azure OpenAI (2.1) | azure_openai | `/openai/v1` compatibility surface, `api-key` header; models = deployment names listed in config |
| Hugging Face Inference Providers, Fireworks AI, DeepInfra (2.1) | openai_compat | shared adapter |
| Ollama, LM Studio, vLLM / self-hosted (2.1) | openai_compat | presets of the custom endpoint with the right default URL; key optional |
| Custom OpenAI-compatible | openai_compat | any other proxy / server |
| TG11 key vault (2.1) | – | not a provider: *Sync from TG11* copies keys stored at accounts.tg11.org into the providers above (needs the `tg11.ai` scope and a trusted client) |
| GitHub Copilot | – | Copilot subscriptions expose no API key; GitHub Models (PAT with `models:read`) is the supported route |

## Adding a provider

1. **OpenAI-compatible vendor** — add a `register(ProviderSpec(...))` block in
   `registry.py`: id, name, docs/console URLs, `credential_fields`,
   `config_fields` (base URL, org…), `default_base_url`, `capabilities`,
   `family_rules` (regex → UI group label). Optionally add fallback models and
   pricing to `catalog.json`. Done.
2. **Native protocol** — add `providers/<vendor>.py` subclassing
   `ProviderAdapter`, implement `_headers`, `list_models`, `chat` (and
   `stream_chat`/`embeddings`/`image_generation` if supported), register the
   class in `providers/__init__.py:ADAPTERS`, then add the spec with
   `adapter="<key>"`.
3. **Multi-field / OAuth credentials** — `credential_fields` is a list of
   `CredentialField(key, label, secret, required, …)`; all secret fields are
   stored together in one encrypted JSON blob, non-secret fields in
   `config_json`. An OAuth-based provider would add a small router to obtain the
   token and then call `credentials.upsert_credential(..., secrets={"access_token": …})`.
4. Errors: return the right `ProviderError` subclass (or rely on
   `map_http_error`) so the UI can show a useful message and usage records get a
   stable `error_kind`.
5. Add a test with `httpx.MockTransport` (see `tests/test_ai_providers.py`).

## Credential security

* AES-256-GCM (`app/security/crypto.py`), 96-bit random nonce, versioned
  envelope `FB1 | key_version | nonce | ciphertext+tag`.
* Key from `FLOWBOARD_CREDENTIAL_ENCRYPTION_KEY`; rotation via
  `FLOWBOARD_CREDENTIAL_ENCRYPTION_KEY_V<n>` + `python -m app.cli rotate-credential-keys`.
* AAD binds each blob to `ai-credential:<user_id>:<provider>` — a blob copied to
  another row/user cannot be decrypted.
* Plaintext exists only inside `credentials.decrypt_secrets` → adapter
  construction. It is never logged, never rendered; the UI shows
  `secret_hint` (`sk-••••••••4X2q`).
* Every credential function takes the owning `User`; there is no lookup by id
  without an owner check.
* Usage records store tokens/latency/cost/error kind only; prompt/response
  text is stored only when `FLOWBOARD_AI_LOG_PROMPTS=true`.

## Pricing / usage

`catalog.json → pricing[provider][model] = [input_usd_per_1M, output_usd_per_1M]`.
Lookup is exact id, then longest prefix (dated snapshots inherit). Missing
entries produce no estimate rather than a wrong one. Update the file when
vendors change prices — no code change required.

## AI features added in 2.1

* **Calibration context** — every assistant / enrichment / instructions prompt gets a short block built from the user's completion reflections (`services/reflections.prompt_summary`): actual÷estimated time ratio (overall and per context), energy delta, clarity and difficulty ratings, recent notes. Nothing is trained; it lives in the context window per request.
* **Schedule context** — the user's per-day capacity and what is already scheduled for the next 7 days (`services/schedule.schedule_summary_for_ai`) so "plan my week" proposals respect real limits. Proposals can set `scheduled_date` on tasks.
* **How do I do this?** — `ai/instructions.py` writes step-by-step instructions for one task and stores them in `Task.instructions` (shown inside the expanded card, rendered with the safe `md` filter).
* **Quick prompts** — Daily briefing, Notes → tasks, Fit my schedule.
