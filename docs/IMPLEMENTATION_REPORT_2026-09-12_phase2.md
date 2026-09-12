# Implementation report — Phase 2 (Flowboard 2.1 + TG11 Accounts 0.2)

Date: 2026-09-12 · Deployed: flowboard.fyi 2.1.1 (commit 34f5bb9), accounts.tg11.org 0.2.1 (commit cd0c62e)

## What was asked
> accounts.tg11.org: header image, profile image, bio, phone number, optional payment holds, buttons to the other TG11 apps, request-to-change-email, AI in accounts too.
> flowboard.fyi: every task list and every task a dropdown; learn from every completed task (how long, how much energy, were the instructions clear…) and use it per user; a default board that teaches the app; more AI integration options; tasks that spill past the daily allotment move to the next free day, with a schedule of max work/energy/hours per day.

## accounts.tg11.org 0.2

| Feature | Where | Notes |
|---|---|---|
| Profile image + header image | `/account` → Avatar / Header upload | PNG/JPEG/WebP ≤ 8 MB, re-encoded with Pillow, served from `/media/…` (`TG11_MEDIA_DIR`) |
| Bio, website, display name, pronouns | `/account` | exposed in the `profile` scope claims (`tg11_bio`, `tg11_header_image`, `picture`, `website`) |
| Phone number | `/account` → Phone | E.164 normalised; verification by SMS code when `TWILIO_*` is set (otherwise stored unverified). `phone` scope → `phone_number`, `phone_number_verified` |
| Email change | `/account` → Change email | signed action token e-mailed to the **new** address; old address keeps working until confirmed; cancel/resend |
| App links | `/account` "Your TG11 apps" | every registered OAuth client with a `home_url` shows as a button (Flowboard, FreeParty, FurryParty, Shop, EchoQuill… as they are registered with `--home-url/--link-url/--icon`) |
| AI key vault | `/vault` | per-user encrypted store (AES-256-GCM, `TG11_VAULT_KEY`) of provider keys; trusted apps with the `tg11.ai` scope read it from `GET /api/v1/ai/credentials` |
| Wallet & payment holds | `/wallet` | providers registry: **Stripe live** (SetupIntent → saved card; manual-capture PaymentIntent = *hold*; capture/release), PayPal/Link/Venmo/Cash App/Airwallex/Adyen/BTC/ETH/**FoxPay** listed as *planned* with what each needs. Apps use `POST /api/v1/payments/holds` (`tg11.payments` scope) — that is what FreeParty will call for deposits |
| API | `/api/v1/me`, `/api/v1/links`, `/api/v1/payments/*`, `/api/v1/ai/credentials` | bearer (user) or basic (client) auth as documented in `docs/API.md` |

Server: `TG11_VAULT_KEY` generated and added to `/var/www/TG11Accounts/.env` (own line). Flowboard client re-registered: trusted, scopes `openid profile email phone tg11.profile tg11.ai offline_access`, home/link URLs. DB backed up before deploy (`data/accounts.sqlite3.bak-v0.1-*`).

## flowboard.fyi 2.1

| Feature | Where | Notes |
|---|---|---|
| Collapsible lists, expandable tasks | board | click a column title or a task title; state remembered per board in the browser; *⊞ all / ⊟ all* |
| Completion reflections | Done → modal | actual minutes, real energy, clarity 1–5, difficulty 1–5, note; Skip/Esc. Feeds `task_reflections` → per-user calibration profile (Settings → Schedule & Calibration) |
| Calibration used by the AI | every assistant / enrich / instructions prompt | "actual time is 1.4× estimates on Coding", energy delta, clarity, notes — in the context window only, per user; `adjusted_estimate` scales estimates once ≥3 samples |
| Work schedule | Settings → Schedule & Calibration | per weekday: on/off, start, end, max minutes, max high-energy minutes; days off; auto-rollover; horizon |
| Capacity-aware scheduling | board *📅 Auto-schedule*, `/boards/<slug>/schedule` week view, per-task *Set day* | never exceeds a day's limits (across all your boards), respects dependencies and due dates, overflow visible not lost |
| Rollover | automatic when a board/schedule opens | unfinished tasks from past days move to the next free day (`↻n` badge) |
| "How do I do this?" | expanded task | assistant writes step-by-step instructions, stored with the task, rated for clarity on completion |
| Tutorial board | 🎓 *Getting started* | 11 sequenced tasks, seeded for new accounts and for existing accounts on next visit; *Recreate* in Settings → Account |
| More AI providers | Settings → AI Providers | + Azure OpenAI, Hugging Face Inference Providers, Fireworks AI, DeepInfra, Ollama, LM Studio, vLLM/self-hosted (27 providers total) |
| More AI features | Assistant chips | *Daily briefing*, *Notes → tasks*, *Fit my schedule*; proposals may set `scheduled_date` |
| TG11 vault sync (two-way, 2.1.1) | Settings → AI Providers → *⬇ Pull from TG11* / *⬆ Push to TG11* | Pull copies vault keys into Flowboard (also automatic on TG11 sign-in with the scope); Push sends all or one provider's key to the vault via `PUT /api/v1/ai/credentials` (TG11 0.2.1). Each direction overwrites the same provider on the receiving side; entries without a secret are skipped, never blanked; TG11 shows *via flowboard* on pushed keys |
| TG11 back-link | on link/sign-up | Flowboard registers the identity link at TG11 (`/api/v1/links`) so TG11's account page can show it |

Migration `0002` adds `task_reflections`, `tasks.scheduled_date/rollover_count/instructions`, profile `work_schedule_json/calibration_json/tutorial_seeded/tg11_vault_synced_at`. It was rewritten to plain `ADD COLUMN` after the first production run failed: Alembic's SQLite batch mode rebuilds the table and the live DB had a profile pointing at a deleted board (FK violation). The migration is now idempotent and cleans up `_alembic_tmp_*` leftovers. DB backed up before deploy (`data/flowboard.sqlite3.bak-v2.0-*`).

Tests: Flowboard 37 passed (9 new), TG11 9 passed. Public smoke test with a throwaway account: register → tutorial board (11 cards) → Done → reflection saved → schedule pages → provider forms → auto-schedule — all 200; account removed afterwards.

## Things I'd suggest next (not built)

1. **Reflection nudges instead of a modal every time** — after ~10 reflections, only ask when the task's estimate was off by >30% or it had AI instructions; otherwise a one-click "took about as long as planned".
2. **Focus timer → reflection** — the Focus button already knows the elapsed time; pre-fill `actual_min` from it and auto-open the reflection.
3. **Calendar-aware capacity** — subtract busy time from connected Google/Microsoft calendars from each day's `max_min` so the schedule reflects meetings.
4. **Energy curve by hour** — you set a start/end per day; letting people mark "mornings = high energy" would let the planner place high-energy tasks in the right slot, not just the right day.
5. **Streaming assistant** — the adapters already support streaming; the UI still waits for the whole proposal.
6. **Shared boards with per-member schedules** — capacity is per user; a shared board could show "who has room this week".
7. **TG11 payments** — wire FreeParty's deposit flow to `/api/v1/payments/holds` first (Stripe), then add PayPal (Orders API with `intent=AUTHORIZE`) since it is the second most requested; crypto holds are really escrow and need a custodial or on-chain design — FoxPay is the natural home for that, and the coin can be added there as a payment method once it has a price feed.
8. **Push/email digest** — the *Daily briefing* prompt could run on a schedule and be emailed/pushed each morning.
9. **Mobile layout pass** — collapsible columns help, but the board is still wide; a single-column mode for phones is a small CSS change.
10. **Backups** — the snapshot script exists; a nightly cron with SHA-256 verification and 14-day retention is a five-line addition.
