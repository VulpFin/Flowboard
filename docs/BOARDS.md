# Board architecture

`boards` is the workspace table: UUID `id`, `owner_id`, `name`, `slug` (unique
**per owner**), `description`, `icon`, `color`, `theme_json`, `is_archived`,
`settings_json`, `position`, timestamps.

Every board-scoped entity carries `board_id`: `tasks`, `task_activity`,
`ai_change_sets`, `calendar_event_links`, `calendar_feed_tokens`.

## URLs

`/boards/<slug>/…` — the slug is resolved **within the current user's
memberships**, so two users can both have `/boards/work/`. A board can also be
addressed by its UUID (`/boards/<uuid>/`). Integer ids are never exposed.

Board-level routes: `/boards/<slug>/` (view), `/settings`, `/activity`,
`/tasks…`, `/plan`, `/plan.ics`, `/export.ics`, `/ai/…`, `/feed/…`.

## Authorization

`services/boards.get_board_for_user(db, user, slug_or_id, require=BoardRole.X)`
is the single gate. It joins through `board_memberships`, and raises
`BoardNotFound` for both "does not exist" and "not yours" (no existence leak),
or `BoardAccessDenied` if the role is insufficient. FastAPI dependencies
`current_board` / `current_board_editor` / `current_board_admin` wrap it.
Tasks are always looked up **through the board** (`tasks.get_task(db, board,
id)`), so a task id from another board resolves to nothing.

## Membership (future shared boards)

`board_memberships(board_id, user_id, role, invited_by_id, accepted)` with
roles `owner > admin > editor > viewer`. The owner always has an `owner` row;
the UI for inviting others is not built yet but nothing in the schema or the
authorization code needs to change.

## Settings inheritance

Board `settings_json.ai_model` → account `UserProfile.default_ai_model` →
first enabled provider default. Board defaults for *new* boards live in
`UserProfile.board_defaults_json` (Settings → Board Defaults).

## Legacy migration

Flowboard 1.x stored all tasks in `flowboard_tasks.json`. `python -m app.cli
import-legacy --file … --user …` creates (or reuses) the owner's default
board and imports every task, preserving ids as `tasks.legacy_id`,
dependencies, contexts, order, completion state, actuals and creation dates.
It is idempotent and never modifies the JSON file.
