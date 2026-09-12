# Board architecture

`boards` is the workspace table: UUID `id`, `owner_id`, `name`, `slug` (unique
**per owner**), `description`, `icon`, `color`, `theme_json`, `is_archived`,
`settings_json`, `position`, timestamps.

Every board-scoped entity carries `board_id`: `tasks`, `task_activity`,
`ai_change_sets`, `calendar_event_links`, `calendar_feed_tokens`.

## URLs and board references (2.3)

Slugs are unique **per owner**, so on a shared board two people can each have a
board called "personal". A board reference is therefore one of:

| form | meaning |
|---|---|
| `personal` | *your own* board with that slug |
| `alice~personal` | the board `alice` owns — the qualified form, unique and stable |
| `<uuid>` | any board, by id |

`boards.ref_for(board, user)` returns the reference that user should follow
(bare slug for your own boards, qualified otherwise) and is what every template
and redirect emits — `{{ board_ref }}` for the current board, `{{ ref_for(b,
user) }}` in lists. `~` is the separator because `slugify` can never produce it.

`get_board_for_user` resolves in this order: UUID → qualified `owner~slug`
(which deliberately does **not** fall back to a board of your own with the same
slug) → a board *you own* with that slug → the oldest board you are a member of
with that slug. Membership and role are enforced afterwards either way, so a
qualified reference to a board you are not on is indistinguishable from one
that does not exist.

Board-level routes: `/boards/<ref>/` (view), `/settings`, `/activity`,
`/tasks…`, `/plan`, `/plan.ics`, `/export.ics`, `/ai/…`, `/feed/…`,
`/members/…`, `/invites/<id>/revoke`.

## Authorization

`services/boards.get_board_for_user(db, user, slug_or_id, require=BoardRole.X)`
is the single gate. It joins through `board_memberships`, and raises
`BoardNotFound` for both "does not exist" and "not yours" (no existence leak),
or `BoardAccessDenied` if the role is insufficient. FastAPI dependencies
`current_board` / `current_board_editor` / `current_board_admin` wrap it.
Tasks are always looked up **through the board** (`tasks.get_task(db, board,
id)`), so a task id from another board resolves to nothing.

## Membership and invitations (2.3)

`board_memberships(board_id, user_id, role, invited_by_id, accepted)` with
roles `owner > admin > editor > viewer`. The owner always has an `owner` row.
A membership row means *someone who said yes*: pending invitations live in
their own table.

`board_invites(board_id, email, role, token_hash, token_prefix, invited_by_id,
message, created_at, expires_at, accepted_at/by, declined_at, revoked_at)`:

* Board admins invite by **email** (Board settings → Members), so the person
  does not need an account yet — they register with that address and the
  invitation is waiting at `/invites`.
* The emailed link carries a 256-bit token; only its SHA-256 is stored. It
  expires after `invites.INVITE_TTL_DAYS` (14), is single use, and `accept()`
  refuses unless the signed-in account *is* the invited address — a leaked link
  gets somebody else nothing. Re-inviting the same address revokes the old
  token. Acceptance from the `/invites` list works without the token because
  being signed in as that address is the same proof.
* Only `owner`/`admin` may invite, change roles or remove members. The owner's
  role cannot be changed and the owner cannot be removed; ownership is not
  transferable from the UI. Any member can **leave** a board they do not own.
* Removing a member (or leaving) unassigns their tasks on that board and clears
  those tasks' day/slot, so the work re-plans against the owner's capacity, and
  the board stops being that person's default.

Roles in practice: `viewer` reads; `editor` adds and changes tasks; `admin`
also manages members. Assignment (`tasks.assigned_to_id`) is what connects
membership to scheduling — see SCHEDULING_AND_CALIBRATION.md for how a task
consumes the assignee's capacity and what the "who has room this week" panel
does (and does not) reveal.

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
