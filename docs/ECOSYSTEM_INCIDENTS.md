# TG11 ecosystem: outage diagnoses and account-system analysis (2026-09-12)

## freeparty.dev — HTTP 503 (fixed)

Cause: the vhost proxied to `127.9.0.0:18000`, the bind address of a *second*
FreeParty docker-compose stack in `/var/www/Freeparty.dev` (compose project
`freeparty-dev`). That stack had no systemd unit, so after the 2026-07-21
reboot its containers stayed down (`Exited (0) 7 weeks ago`) and Apache got
"Connection refused" → 503. `freeparty.tg11.org` runs from `/var/www/Freeparty`
on `127.5.0.0:18000` and was unaffected.

Fix (freeparty.dev is meant to be the *same* application):
`freeparty.dev.conf` now proxies to `127.5.0.0:18000` with static/media from
`/var/www/Freeparty`; `freeparty.dev` + `www.freeparty.dev` were added to
`ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `CORS_*` in `/var/www/Freeparty/.env`
(backup `.env.bak.20260912`), and only the `web` container was recreated to
pick the env up (db/redis/celery untouched). `SITE_DOMAIN` stays
`freeparty.tg11.org` (canonical). Verified: https://freeparty.dev/ → 200.

## furryparty.xyz — HTTP 400/500 (deployed)

Cause: there was **no deployment at all** for furryparty.xyz on this server —
no vhost, no stack. Requests fell through to the default vhost
(bettercorporatelogowear.org, Django) → 400 DisallowedHost (the earlier 500s
came from the same place).

Resolution (per your choice): the idle `freeparty-dev` stack became
**Federation 1**. Its database (`freeparty-dev_postgres_data`, 1 account:
yours from May) was dumped first, the checkout was moved from `46303ad`
(4.6-CS) to the same commit as main (`287ed69`, 4.7-UPDATE) so both
federations run identical code, `.env` now has `SITE_DOMAIN=furryparty.xyz`
and matching host/origin lists (own `SECRET_KEY`, own `FEDERATION_SHARED_SECRET`
as before, bind `127.9.0.0`, compose project name kept so the volume is
reused). New `furryparty.service` (enabled), vhost `furryparty.xyz.conf`
(Let's Encrypt cert, static/media from `/var/www/Freeparty.dev`). Verified:
https://furryparty.xyz/ → 200. Note the app log warns "models in `accounts`
have changes not reflected in a migration" — that's upstream code state, same
on main.

Follow-ups for you: decide whether the `FEDERATION_SHARED_SECRET` values of
the two federations should match (federation inbound/outbound is enabled on
both); consider a `furryparty-db-backup.timer` like the one FreeParty has.

## Account systems today

| app | framework | user model | ids | notes |
|---|---|---|---|---|
| FreeParty / FurryParty | Django 5.1 | custom `accounts.User(AbstractBaseUser)` — **UUID PK**, email login, username, `state`, verification + lifecycle tokens, TOTP, recovery codes | UUID | canonical shape; TG11 Accounts mirrors it |
| Shop (storefront) | Django 6 + allauth 65 | `CustomUser(AbstractUser)` email-only | int | needs a UUID identity column + `IdentityLink`; allauth's OpenID Connect provider can point at accounts.tg11.org |
| Echoquil | Django ≥5.2 (daphne) | default `auth.User` | int | needs an `accounts` app: `UserIdentity(uuid ↔ user)` + `IdentityLink`; adopt FreeParty's state/verification model incrementally |
| GridGoblin | Django 5.0 + allauth | (login not enabled yet) | int | start as OIDC RP only |
| CircuitSmith | docker (nginx) | none | – | start as OIDC RP only |
| Flowboard | FastAPI | 2.0: UUID users, Django-compatible hashes, `identity_links`, TG11 OIDC live | UUID | done |
| Boundless | docker Django | on hold | – | documented only |

Migration mechanics for the Django apps (no re-registration): add a nullable
`tg11_user_id` UUID column + `IdentityLink` table; on first "Sign in with
TG11", link by *verified* email or via an explicit link page; keep local
password login during transition; TG11 Accounts can import Django
`pbkdf2_sha256` hashes verbatim when accounts are migrated server-side. See
`TG11_SSO.md`.
