# Changelog

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
