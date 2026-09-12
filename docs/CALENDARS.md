# Calendar integration

Three tiers, all provider-agnostic at the data-model level.

## 1. iCalendar (works everywhere: Apple Calendar/iOS, Outlook, Thunderbird, Google "from URL")

| URL | what |
|---|---|
| `GET /boards/<slug>/tasks/<id>.ics` | one task |
| `GET /boards/<slug>/export.ics?include_done=0&start=YYYY-MM-DD&end=YYYY-MM-DD` | board (optionally a date range) |
| `GET /boards/<slug>/plan.ics?ids=…&window=…` | the planner's sequential schedule starting now |
| `GET /calendar/feed/<token>.ics` | subscription feed (unauthenticated; the 256-bit token is the credential) |

Mapping (`app/calendars/ics.py`): a task with a date-only due becomes an
all-day `VEVENT`; date+time due becomes a timed `VEVENT` ending at the due
time and lasting `estimate_min`; tasks without a due date are `VTODO`s. UIDs
are stable (`task-<uuid>@flowboard.fyi`) so re-imports update rather than
duplicate. `SEQUENCE` increases with `updated_at`.

Feeds: Settings → Calendars → *Create feed* per board. Only the SHA-256 of the
token is stored; rotate or revoke at any time. The feed URL contains no user or
board id. Refresh hint is 30 min (`REFRESH-INTERVAL`).

## 2. Google Calendar (OAuth 2.0 + PKCE)

Setup (operator, once):
1. Google Cloud console → APIs & Services → enable **Google Calendar API**.
2. OAuth consent screen (external, add yourself as test user until verified).
3. Credentials → OAuth client ID → *Web application* → authorised redirect URI
   `https://flowboard.fyi/calendar/connect/google/callback`.
4. Put `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in `.env`, restart.

Scopes requested: `calendar.events`, `calendar.readonly` (to list calendars),
`openid email` (to label the account). No password is ever asked for.

## 3. Microsoft 365 / Outlook (Microsoft Graph)

1. entra.microsoft.com → App registrations → New → *Web* redirect URI
   `https://flowboard.fyi/calendar/connect/microsoft/callback`.
2. API permissions (delegated): `Calendars.ReadWrite`, `User.Read`,
   `offline_access`. Grant consent as needed.
3. Certificates & secrets → client secret → `.env`
   (`MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MICROSOFT_TENANT_ID=common`
   for personal + work accounts, or your tenant id).

## Data model (`app/models/calendar.py`)

* `CalendarConnection` — user, provider, account email/id, **encrypted token
  blob** (AES-GCM, AAD `calendar-token:<user>:<provider>`), scopes, target
  calendar, cached calendar list, status.
* `CalendarEventLink` — task ↔ (connection, external calendar id, external
  event id), `sync_status` (synced/pending/error/orphaned), `sync_direction`
  (`push`), `last_synced_at`, `last_error`, content `fingerprint`.
* `CalendarFeedToken` — board, user, token hash, prefix, revoked_at.

## Behaviour (deliberately one-way)

1. **Add to calendar**: on a dated task, choose a connection and click
   *+ Calendar* → event created, link stored.
2. **Updates**: when a task changes (title, due, estimate, done…) every linked
   event is updated (`links.sync_task_links`), guarded by a fingerprint so
   unchanged tasks make no API calls. Errors are recorded on the link and shown
   as a red 📅 tag; they never block the UI.
3. Removing the due date deletes the external event and marks the link
   `orphaned`. Deleting the task deletes linked events. Disconnecting an
   account **keeps** events already in the calendar.
4. Nothing flows from the calendar back into Flowboard (no two-way sync yet).

Adding a provider: subclass `CalendarProvider` in
`app/calendars/providers.py` (authorize_url / exchange_code / refresh /
account_info / list_calendars / create_event / update_event / delete_event) and
add it to `CALENDAR_PROVIDERS`. Nothing else changes.
