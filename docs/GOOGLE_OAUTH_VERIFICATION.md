# Google OAuth Verification Checklist

This guide describes Flowboard 2.x as implemented. It does not contain or
create reviewer credentials.

## Public URLs

* Homepage: `https://flowboard.fyi/`
* Privacy Policy: `https://flowboard.fyi/privacy`
* Terms of Service: `https://flowboard.fyi/terms`
* Third-Party Services: `https://flowboard.fyi/third-party-services`
* Reviewer guide: `https://flowboard.fyi/oauth-review`
* Google callback: `https://flowboard.fyi/calendar/connect/google/callback`

## Google Calendar scopes

The production authorization URL requests exactly these Google Calendar API
scopes, defined in `app.calendars.providers.GOOGLE_CALENDAR_SCOPES`:

* `https://www.googleapis.com/auth/calendar.readonly` - list available
  calendars and retrieve free/busy availability for Flowboard scheduling.
* `https://www.googleapis.com/auth/calendar.events` - create, update, and
  delete an event when the user explicitly acts on a dated Flowboard task.

No Google OpenID, profile, Drive, Gmail, or other Google scopes are requested.

## Provision a reviewer account

Flowboard is FastAPI, not Django, so it has no Django admin. Provision a normal
local account from the production host with the existing CLI:

```bash
cd /var/www/Flowboard/app
/var/www/Flowboard/.venv/bin/python -m app.cli create-user \
  --email reviewer@example.com --username flowboard-reviewer
```

Enter the password only at the secure terminal prompt. The command creates an
active, email-verified, non-staff account, creates its profile/default board,
and seeds its onboarding tutorial. Do not use `--staff`. Flowboard has no
subscription, payment, invitation, phone verification, or MFA gate for this
ordinary account. Store the reviewer password outside source control and supply
it directly to Google only when requested.

## Reproduce the demonstrated behavior

1. Sign in at `https://flowboard.fyi/login` with the reviewer account.
2. Open Settings, then Calendars & integrations.
3. Choose Connect Google Calendar, read the local explanation, then continue
   to Google's consent screen.
4. Grant the two listed Calendar scopes and return to Flowboard.
5. Select Refresh calendars and Show availability. The latter displays actual
   free/busy intervals for the next seven days but deliberately never shows
   event titles or descriptions.
6. Open the default board, create a dated task, select the Google connection,
   and choose + Calendar. Confirm the created event in Google Calendar.
7. Edit the task and confirm the remote event changes. Remove its due date to
   demonstrate deletion of the linked event if that is appropriate for the
   recording.

## Verification video checklist

1. Open the logged-out homepage and show Flowboard's product description.
2. Open Privacy and Third-Party Services without logging in.
3. Sign in with the ordinary reviewer account.
4. Navigate to Settings -> Calendars & integrations -> Connect Google Calendar.
5. Show Google consent in English. Expand any collapsed scope list and show
   both requested Calendar scopes before approving.
6. Return to Flowboard, refresh the calendar list, and show availability.
7. Create a dated Flowboard task, add it to Google Calendar, and show the event
   in the connected Google Calendar account.
8. Update or remove the linked task event in Flowboard and show the equivalent
   change in Google Calendar.
9. Return to Flowboard and show that the integration remains connected.

## Manual Google Cloud Console work after deployment

1. Confirm `flowboard.fyi` is a verified/authorized domain in Google Cloud.
2. Set the OAuth consent screen homepage to `https://flowboard.fyi/`.
3. Set the privacy URL to `https://flowboard.fyi/privacy` and the terms URL to
   `https://flowboard.fyi/terms`.
4. Register the exact callback URL above and enable the Google Calendar API.
5. Confirm the consent screen lists only the two scopes above, then save.
6. Record and upload the verification video, prepare the reviewer credentials
   outside source control, and reply to Google's verification request. Do not
   email credentials or submit verification material automatically from this app.
