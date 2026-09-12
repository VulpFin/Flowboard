# Scheduling, rollover and per-user calibration (Flowboard 2.1)

## Work schedule
Settings → Schedule & Calibration stores, per weekday: enabled, start/end, **max minutes**
and **max high-energy minutes**, plus explicit days off, `auto_rollover` and a horizon
(7–90 days). Stored as JSON in `user_profiles.work_schedule_json`
(`services/schedule.load_schedule` fills defaults: Mon–Fri 09:00–17:00 / 480 / 180, weekends off).

## Day placement
`Task.scheduled_date` is *the day you plan to work on it* (distinct from `due`).
`auto_schedule(db, user, board)` walks open tasks in planner priority order
(due date, then `priority_score`), honouring dependencies (never before a dependency's
day) and the capacity of every day in the horizon **across all of the user's boards**.
A task that fits no single day (bigger than any day's limit) is placed on the first
enabled day and shows as over-capacity rather than being dropped.

Buttons: board → *Auto-schedule* (unscheduled only), schedule page → *Re-plan all*,
per-card *Set day*, schedule page *Move*.

## Rollover
`rollover(db, user, board)` runs lazily whenever an editor opens the board or the
schedule page (and via *Roll over now*): unfinished tasks whose day has passed lose
their day, get `rollover_count += 1`, and are re-placed by `auto_schedule` on the next
day with room. Turn it off with the *auto rollover* checkbox.

## Completion reflections → calibration
Marking a task Done opens a short form (Esc / Skip to dismiss): actual minutes, actual
energy, clarity 1–5, difficulty 1–5, free-text note. Rows go to `task_reflections`;
`services/reflections.refresh_profile` recomputes `user_profiles.calibration_json`:

* `time_ratio` (+ per context, n≥3 to be used) → `time_multiplier` bounded 0.5–2.0 and
  `adjusted_estimate`
* `energy_delta` (+1 = tasks feel harder than planned)
* `clarity_avg`, `clarity_with_instructions_avg`, `difficulty_avg`, `recent_notes`

`prompt_summary(user)` renders this as a few lines that are appended to every AI
request for that user (assistant, enrichment, instructions). It is per user, lives only
in the prompt, and can be cleared from the settings page.

## Tutorial board
`services/tutorial.seed_tutorial` creates the 🎓 *Getting started* board (11 sequenced
tasks, one per feature) on sign-up (local and TG11) and from Settings → Account →
*Recreate tutorial board*.
