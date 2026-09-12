# Flowboard 2.0 / TG11 rollout — implementation report (2026-09-12)

## Implemented

* **Flowboard 2.0** (FastAPI + SQLAlchemy/Alembic, SQLite, HTMX UI, same dark theme):
  accounts with login/registration/password reset/sessions; **multiple boards per
  account** with `/boards/<slug>/` URLs and a switcher; tasks/planner/focus mode
  preserved; activity history; unified **Settings** (Account, AI Providers, AI
  Defaults, AI Usage, Calendars & Integrations, Security, Board Defaults).
* **BYO AI providers**: 20 provider specs behind one adapter interface, AES-256-GCM
  encrypted credentials, test/enable/disable/replace/remove, model enumeration +
  cache, grouped model picker limited to configured providers, capability tags,
  `provider:model` registry, board → account → provider-default inheritance,
  explicit opt-in fallback, per-call usage tracking with cost estimates
  (`app/ai/catalog.json`).
* **AI assistant** on every board: structured proposals (create/update/complete/
  reopen/delete) reviewed and applied per-operation by the user; quick prompts
  (summarise, prioritise, overdue/blocked, plan week, estimates); task
  enrichment (contexts/estimate/importance/energy) via the same abstraction —
  works with providers lacking tool calling through validated JSON fallback.
* **Calendars**: per-task / per-board / plan `.ics`, tokenised subscription
  feeds (rotate/revoke), Google Calendar and Microsoft 365 OAuth connections
  with one-way push + update + unlink (code complete; needs your OAuth apps).
* **TG11 identity**: UUID users, Django-compatible password hashes,
  `identity_links`, OIDC relying-party (code + PKCE, JWKS validation), account
  linking rules, "Sign in with TG11" button, link/unlink in Settings → Security.
* **TG11 Accounts** identity provider deployed at **https://accounts.tg11.org**
  (`U:\Projects\TG11Accounts`), Flowboard registered as trusted client; SSO
  verified end-to-end in production.
* **Domain migration** to https://flowboard.fyi with 301s from the old host.
* **freeparty.dev** 503 fixed; **furryparty.xyz** deployed as Federation 1.

## Database migrations

New SQLite database `/var/www/Flowboard/data/flowboard.sqlite3`, Alembic
revision `0001`: `users`, `user_profiles`, `identity_links`,
`password_reset_tokens`, `user_sessions`, `boards`, `board_memberships`,
`tasks`, `task_activity`, `ai_provider_credentials`, `ai_usage`,
`ai_change_sets`, `calendar_connections`, `calendar_event_links`,
`calendar_feed_tokens`. Flowboard 1.x had no database and no users: its
`flowboard_tasks.json` (45 tasks) was imported into the owner's default board
"Personal" (`python -m app.cli import-legacy`, idempotent, legacy ids kept in
`tasks.legacy_id`, dependencies/contexts/order/actuals/created dates preserved).
The JSON file was left untouched. TG11 Accounts uses its own SQLite db
(`/var/www/TG11Accounts/data/accounts.sqlite3`). No production database was
reset or recreated; FreeParty/FurryParty databases were only dumped.

## New configuration (you must supply)

`/var/www/Flowboard/.env` (documented in `.env.example`, `docs/ENVIRONMENT.md`):
generated `FLOWBOARD_SECRET_KEY`, `FLOWBOARD_CREDENTIAL_ENCRYPTION_KEY`, TG11
OIDC client credentials — all set. **Empty until you create the apps:**
`GOOGLE_CLIENT_ID/SECRET` (redirect `https://flowboard.fyi/calendar/connect/google/callback`),
`MICROSOFT_CLIENT_ID/SECRET/TENANT_ID` (redirect `…/calendar/connect/microsoft/callback`).
SMTP for password-reset mail reuses the `noreply@tg11.org` credentials from
FreeParty's `.env` (both Flowboard and TG11 Accounts) — say if you'd rather not.

## AI providers

Shared OpenAI-compatible adapter: OpenAI, xAI, Groq, GitHub Models, Together,
Mistral, OpenRouter, DeepSeek, Moonshot/Kimi, Cerebras, Perplexity (no model
list → catalog), Novita, MiniMax (no model list → catalog), OctoAI (legacy —
public service shut down 2024), Custom OpenAI-compatible (Ollama/LM Studio/
vLLM). Native adapters: Anthropic, Google Gemini, Cohere, Stability AI (images
only), Replicate (predictions; curated + pinned models). GitHub Copilot itself
has no API key — GitHub Models is the supported route. Adapters were exercised
with mocked HTTP; real-key smoke tests are yours to run from Settings → AI
Providers → *Save & test*.

## Calendar support

ICS export/feeds: live now (Apple/iOS, Outlook, Thunderbird, Google via URL).
Google Calendar & Microsoft 365: implemented and unit-tested with a fake
provider; buttons appear as soon as the OAuth client ids are in `.env`.
Sync is deliberately one-way (Flowboard → calendar) — see `docs/CALENDARS.md`.

## TG11 ecosystem

Now: OIDC provider live (discovery, JWKS/RS256, authorize with PKCE, token,
refresh rotation, userinfo, revoke, RP logout, consent for third-party
clients, account page with sessions/apps, admin CLI for clients/users/legacy
links). Flowboard is the first client. Remaining: email-verification
enforcement (flag), TOTP/passkeys/recovery codes (schema-ready), admin UI,
OIDC-RP work in FreeParty/Shop/Echoquil/FurryParty/GridGoblin/CircuitSmith
(plan and per-app notes in `docs/TG11_SSO.md`, `docs/ECOSYSTEM_INCIDENTS.md`).
Shop and Echoquil were analysed, not modified.

## Production / domain

Apache: `flowboard.fyi.conf` (new; static via Apache, ACME path, HSTS),
`flowboard.conf` (now a 301 redirect for flowboard.vulpfin.com, path+query
kept), `accounts.tg11.org.conf`, `furryparty.xyz.conf` (new),
`freeparty.dev.conf` (backend switched). All applied with `apachectl
configtest` + graceful reload; no firewall changes. TLS: flowboard.fyi uses
your Cloudflare Origin cert (LE cert also issued as fallback); furryparty.xyz
uses Let's Encrypt; renewals fixed via `/opt/certbot-venv` (system certbot is
broken by a pyOpenSSL/cryptography mismatch). Services: `flowboard.service`
(now runs as unprivileged `flowboard` user, hardened), `tg11-accounts.service`,
`furryparty.service`. Verified publicly: new domain 200 over HTTPS, static
files, HEAD requests, old-domain 301s, login, board isolation, AI settings
page, no errors in logs.

## Security

API keys/OAuth tokens: AES-256-GCM, key from env, per-owner AAD, versioned for
rotation, masked in UI, never logged. Board permissions: membership-based
authorization with no existence leaks; tasks resolved through boards;
credentials/connections/links resolved through the owning user. Sessions:
server-side, signed cookie, revocable; CSRF token + Origin check; Django-grade
PBKDF2 hashes; feed tokens hashed; TrustedHost + security headers.

## Tests

Flowboard: 28 tests (`tests/`) — crypto/AAD/rotation, Django-compatible
hashes, board isolation (service + HTTP), CSRF, provider registry/dispatch/
error mapping, structured-output fallback with repair, model inheritance,
ICS shapes, feed token security, calendar ownership/encryption, fake-provider
push/sync, legacy import, full page smoke + assistant change-set flow.
TG11 Accounts: 5 tests including a full code+PKCE flow using Flowboard's own
OIDC client. All passing. Production: manual curl/Playwright validation
(login, board, redirects, SSO round trip).

## Manual setup remaining

1. Change the temporary Flowboard password (Settings → Security).
2. Cloudflare: set flowboard.fyi SSL mode to Full (strict); optionally add a
   `www` record.
3. Register your TG11 account at accounts.tg11.org (verify the email link),
   then link it in Flowboard Settings → Security.
4. Create Google / Microsoft OAuth apps and fill the `.env` keys.
5. Add your AI provider keys in Settings → AI Providers.
6. Decide the `FEDERATION_SHARED_SECRET` policy between the two FreeParty
   federations; consider a DB backup timer for furryparty.
7. The `.claude-bridge/` folder in the project can be deleted when done.

## Files

Flowboard (`U:\Projects\Vulpfin Flowboard`, git): `app/` (config, db, models/,
security/, services/, ai/ + providers/, calendars/, identity/, web/routers/,
templates/, static/), `migrations/`, `tests/`, `deploy/`, `docs/`,
`.env.example`, `requirements*.txt`, `legacy/` (1.x sources). TG11 Accounts
(`U:\Projects\TG11Accounts`, git): `tg11/` (config, models, oidc, accounts,
web, cli, templates), `tests/`, `deploy/`. Backups: see `docs/ROLLBACK.md`.
