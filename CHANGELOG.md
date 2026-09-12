# Changelog

## 2.3.0 — 2026-09-12

* **Board invitations.** Board settings → Members: invite by email with a role, see and revoke pending invitations, change a member's role, remove a member, and (as a member) leave a board. The invited person does not need an account yet — they register with that address and the invitation is waiting at `/invites`, also shown as a ✉ badge in the top bar. The emailed link carries a 256-bit token stored only as a hash: 14-day expiry, single use, and it can only be redeemed by the address it was sent to. Re-inviting rotates the token; removing a member (or leaving) unassigns their tasks on that board so the work re-plans against the owner's capacity.
* **Unambiguous board URLs.** Slugs are unique per owner, so on a shared board two people could each have `/boards/personal/`. Your own boards keep the bare slug; a board someone else owns is addressed as `owner~slug` (`/boards/alice~personal/`), which every link, form and redirect now emits. A qualified reference never falls back to a board of your own with the same slug, and membership is still enforced, so it leaks nothing.
* A board you are only a *member* of can no longer become your default board.
* Migration `0004` (`board_invites`).

## 2.2.1 — 2026-09-12

* The focus timer and the reflection nudge now agree: when the timer measured the real duration, the one-click nudge records *those* minutes ("you focused for 52 min — record that?") instead of the estimate, and *No, tell me more* carries the measured value into the full form.
* Mobile: the nudge puts its question on its own line so the three buttons fit side by side.
* `.gitattributes` keeps `*.sh`, `*.service` and `*.timer` LF-only — CRLF endings on `deploy/deploy.sh` made the 2.2.0 deploy abort at `set -o pipefail`.

## 2.2.0 — 2026-09-12

* **Reflection nudges.** After 10 reflections the full questionnaire only opens when the answer is likely to matter — the context is off by more than 30%, the task carried AI instructions, or it was ≥ 90 minutes. Otherwise a one-line prompt asks *"took about as long as planned?"*; **Yes** records a reflection with `actual_min = estimate` in one click, **No, tell me more** opens the full form. Settings → Schedule & Calibration → *Always ask the full questions* restores the old behaviour.
* **Focus timer → reflection.** When the timer stops or finishes it asks *"mark done?"*; if you say yes the task is completed and the elapsed minutes pre-fill `actual_min` in the reflection. The old `prompt()` "how many minutes?" dialog is gone.
* **Calendar-aware capacity.** Busy time from connected Google/Microsoft calendars (Google `freeBusy`, Microsoft `calendarView`) is subtracted from each day's limit, cached 15 minutes per connection, and shown as *− 90 min meetings* on the week view and in the AI context. Auto-schedule never plans work on top of a meeting. Off per connection (Settings → Calendars) or account-wide (Settings → Schedule). Any calendar failure is silent: you keep the full day.
* **Energy curve.** Each weekday now has high / medium / low energy blocks (defaults: morning high, afternoon medium, evening low). Auto-schedule places tasks into blocks that match their energy and writes `scheduled_start` (HH:MM), which the board, week view and ICS/plan exports use. Reflections record the hour of day; after 15 of them a *learned* curve is shown next to the declared one with a *Use learned curve* toggle.
* **Shared boards.** Tasks can be assigned to a board member (`assigned_to_id`); auto-scheduling then uses *that* member's schedule, calendar and workload. Boards with more than one member get a *Who has room this week* panel — free minutes per member per day, never their other boards' task titles — and the assistant is told member names, ids and free minutes.
* **Morning digest.** Opt-in per user (Settings → Morning Digest): local hour, timezone, which boards, "only when something is due". `python -m app.cli send-digests` runs the *Daily briefing* prompt through your own AI provider (plain non-AI digest when no provider is configured) and emails it; a systemd timer (`flowboard-digest.timer`) runs every 15 minutes and `last_sent_on` keeps it to one a day. Delivery goes through a channel registry so web push can be added later.
* **Mobile layout.** Below 700px: single-column board with only the first list open, sticky top bar with the board switcher and a ☰ menu, add-task form folded behind a button, assistant below the board, 44px tap targets on card actions, full-width reflection modal and nudge.
* Migration `0003` (tasks.scheduled_start / assigned_to_id, task_reflections.hour_of_day, user_profiles.digest_json, calendar_connections.busy_enabled / busy_cache_json / busy_fetched_at) — plain `ADD COLUMN`, idempotent, no table rebuilds.

## 2.1.2 — 2026-09-12

* Fix: the task *Edit* button rendered an empty card — the board section's `hx-select="#board"` was inherited by child htmx requests and filtered the edit form away. Added `hx-disinherit`.

## 2.1.0 — 2026-09-12

* Board UI: every context column collapses, every task card expands (state remembered per board in the browser); *expand/collapse all*.
* Completion reflections: on Done, a short questionnaire (actual time, real energy, clarity, difficulty, notes) feeds a per-user calibration profile that is injected into every AI prompt and scales estimates (Settings → Schedule & Calibration).
* Work schedule & capacity-aware day planning: per-weekday limits, days off, auto-schedule, week view (`/boards/<slug>/schedule`), automatic rollover of unfinished work to the next free day.
* Per-task *How do I do this?* instructions written by the assistant and stored with the task.
* Tutorial board *Getting started* seeded for every new account; recreatable from Settings.
* More AI: Azure OpenAI, Hugging Face Inference Providers, Fireworks AI, DeepInfra, Ollama, LM Studio, vLLM/self-hosted presets; quick prompts *Daily briefing*, *Notes → tasks*, *Fit my schedule*; assistant can set `scheduled_date`.
* TG11: *Sync from TG11* imports AI keys from the accounts.tg11.org vault (`tg11.ai` scope); identity links are registered back with TG11.
* Migration `0002` (task_reflections, tasks.scheduled_date / rollover_count / instructions, profile schedule + calibration columns).
