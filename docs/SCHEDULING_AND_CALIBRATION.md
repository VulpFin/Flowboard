# Scheduling, rollover and per-user calibration (Flowboard 2.4)

## Work schedule
Settings → Schedule & Calibration stores, per weekday: enabled, start/end, **max minutes**,
**max high-energy minutes** and the two **energy boundaries** (`high_end`, `medium_end`),
plus explicit days off, `auto_rollover`, a horizon (7–90 days) and three switches:
`calendar_busy`, `always_full_reflection`, `use_learned_curve`. Stored as JSON in
`user_profiles.work_schedule_json` (`services/schedule.load_schedule` fills defaults:
Mon–Fri 09:00–17:00 / 480 / 180, weekends off).

## Energy curve
`declared_blocks(day_cfg)` turns one weekday into `[(start_min, end_min, level)]`:
work is **high** energy from the start until `high_end`, **medium** until `medium_end`,
**low** after that. Empty boundaries default to the first half of the day high, then
medium, the rest low.

`reflections.learned_curve(user)` derives an observed curve from `actual_energy` vs the
hour a task was finished (`task_reflections.hour_of_day`, local to the user's timezone).
It needs `CURVE_MIN_SAMPLES` (15) stamped reflections and grades each hour against the
user's own mean, so a uniformly calm week does not collapse to "all low". Hours where
finished work was reported as high-energy are read as hours this person *can* do
demanding work in. Settings shows declared and learned side by side; the
*Use learned curve* toggle makes `energy_blocks()` return the learned one.

## Day placement
`Task.scheduled_date` is *the day you plan to work on it* (distinct from `due`) and
`Task.scheduled_start` is the **HH:MM slot inside that day**.
`auto_schedule(db, user, board)` walks open tasks in planner priority order (due date,
then `priority_score`), honouring dependencies (never before a dependency's day) and the
capacity of every day in the horizon **across all boards of whoever the task belongs to**.
Inside a day, `DayCapacity.place()` searches in 15-minute steps for the earliest free
window whose energy level matches the task: high-energy work tries high blocks first
(then high+medium, then anywhere), medium tries medium, low tries low. Meetings and
already-placed tasks are treated as occupied. A task that fits no single day is placed on
the first enabled day and shows as over-capacity rather than being dropped.

Buttons: board → *Auto-schedule* (unscheduled only), schedule page → *Re-plan all*,
per-card *Set day* (+ time), schedule page *Move*.

## Calendar-aware capacity
`calendars/busy.py` asks each enabled connection for its busy intervals over the horizon
(Google `POST /freeBusy`, Microsoft `GET /me/calendarView`, skipping all-day events and
anything the user is marked *free* for), merges them, clips them to each weekday's
working window and subtracts the result from that day's limit:

    DayCapacity.max_min        the declared limit
    DayCapacity.busy_min       meeting minutes inside the window
    DayCapacity.available_min  max_min - busy_min      <- what scheduling uses
    DayCapacity.busy_slots     minute ranges kept free when placing tasks

The result is cached on the connection (`busy_cache_json`, `busy_fetched_at`,
`CACHE_TTL_SEC` = 15 min). **Every failure is silent** - no connection, an expired token,
a provider outage or the account-wide switch being off all yield `{}`, i.e. the full
declared capacity. Turn it off for one calendar in Settings → Calendars, or for the whole
account with *Subtract meetings…* in Settings → Schedule.

## Ownership on shared boards
`effective_owner_id(task, board) = task.assigned_to_id or board.owner_id`. Capacity,
rollover, the week view and the digest all use it, so assigning a task moves it onto the
assignee's schedule, calendar and workload (its day/slot are cleared so it is re-planned).
Boards with more than one member show *Who has room this week*:
`free_minutes_by_day(db, member, start, days)` returns **minutes only** for each member -
free, used, available and meeting minutes - never the tasks those minutes came from.
`team_summary_for_ai` puts the same numbers (plus member ids) into the AI context, and
the assistant may propose `assigned_to_id`, validated against the board's membership.

## Rollover
`rollover(db, user, board)` runs lazily whenever an editor opens the board or the
schedule page (and via *Roll over now*): unfinished tasks whose day has passed lose their
day and slot, get `rollover_count += 1`, and are re-placed by `auto_schedule` on the next
day with room. Turn it off with the *auto rollover* checkbox.

## Completion reflections → calibration
Marking a task done records a reflection. Which form you get depends on how much we
already know (`reflections.should_ask_full`):

| situation | what opens |
|---|---|
| fewer than `NUDGE_AFTER` (10) reflections | the full questionnaire |
| *Always ask the full questions* is on | the full questionnaire |
| task had AI instructions, or `estimate_min ≥ 90` | the full questionnaire |
| the task's context is off by more than 30% (`OFF_BY`) | the full questionnaire |
| otherwise | the one-line nudge |

The nudge is *"took about as long as planned?"*: **Yes** posts `quick=1` and stores a
reflection with `actual_min = estimate_min` (so the ratio keeps learning from a single
click), **No, tell me more** posts to `…/reflect/full` and swaps in the questionnaire,
**✕** / Esc skips. The focus timer's *mark done?* passes its elapsed minutes to
`/tasks/{id}/done`, which stores them as `last_actual_min` and pre-fills `actual_min`.

Rows go to `task_reflections`; `services/reflections.refresh_profile` recomputes
`user_profiles.calibration_json`:

* `time_ratio` (+ per context, n≥3 to be used) → `time_multiplier` bounded 0.5–2.0 and
  `adjusted_estimate`
* `energy_delta` (+1 = tasks feel harder than planned)
* `clarity_avg`, `clarity_with_instructions_avg`, `difficulty_avg`, `recent_notes`
* `energy_by_hour` / `energy_hour_samples` → the learned energy curve

`prompt_summary(user)` renders this as a few lines appended to every AI request for that
user (assistant, enrichment, instructions), alongside `schedule_summary_for_ai`, which now
also carries meeting minutes, free minutes, today's energy curve and - on a shared board -
the member table. It is per user, lives only in the prompt, and can be cleared from the
settings page.

## Morning digest
Settings → Morning Digest (`user_profiles.digest_json`): `enabled`, `hour` (local),
`boards` (empty = all), `only_if_due`, `channels`, `last_sent_on`. The timezone field
writes the account's `profile.timezone`.

`services/digest.py` gathers today's scheduled tasks, what is due today, what is overdue,
the day's remaining capacity (meetings included), the best task to start with, and - since
2.4 - **what somebody else assigned to you since the last digest** ("2 tasks were assigned
to you on Launch by Priya"). `Task.assigned_at` / `assigned_by_id` are stamped by
`tasks.update_task` whenever the assignee changes, so the UI and the assistant both feed it;
self-assignments are not reported, the window is the last digest (24 h for a first one,
never more than a week), and a new assignment alone is enough to send an
*only when something is due* digest. With an
AI provider configured it runs the assistant's *Daily briefing* prompt through the user's
own credentials and appends the plain digest; without one (or on any provider error) the
plain digest is the whole email. Delivery goes through `CHANNELS` (`email` today - add an
entry plus a checkbox for web push).

    python -m app.cli send-digests [--user a@b.c] [--force] [--dry-run] [--no-ai] [-v]

A user is due when their local time is between their chosen hour and `CATCH_UP_MINUTES`
(3 h) after it, and `last_sent_on` is not today. On the server, `flowboard-digest.timer`
runs the command every 15 minutes:

    systemctl enable --now flowboard-digest.timer
    systemctl list-timers flowboard-digest.timer
    journalctl -u flowboard-digest.service -n 50

## Tutorial board
`services/tutorial.seed_tutorial` creates the 🎓 *Getting started* board (11 sequenced
tasks, one per feature) on sign-up (local and TG11) and from Settings → Account →
*Recreate tutorial board*.
