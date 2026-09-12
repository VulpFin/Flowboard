# TG11 identity / SSO architecture

Goal: one TG11 identity ("Sign in with TG11") across `*.tg11.org`,
`*.vulpfin.com`, `flowboard.fyi`, `freeparty.dev`, `furryparty.xyz`,
`boundless.onl`, `gridgoblin.net`, while every application stays independently
deployable and owns its own data.

## Standards

* OpenID Connect Core 1.0 on OAuth 2.0 — **authorization code flow with PKCE
  (S256)** for every client (server-side apps also send a client secret).
* Discovery (`/.well-known/openid-configuration`) and JWKS (RS256 ID tokens).
* No shared cookies across domains: each app redirects to the identity
  provider and gets back an ID token. Cross-domain SSO is achieved by the
  provider's own session at `accounts.tg11.org`.
* Issuer/host is never hard-coded: apps read `TG11_OIDC_ISSUER`.

## Identity vs. application data

```
TG11 Identity (accounts.tg11.org)         Application-local profile (each app)
------------------------------------      -----------------------------------------
sub      = usr_<uuid> (immutable)         tg11_user_id (FK to sub)
email, email_verified                     app username (if any)
username / display name                   preferences, settings
password hash (Django pbkdf2_sha256)      roles/permissions within the app
MFA (TOTP, passkeys), recovery codes      content ownership (boards, orders, posts…)
sessions/devices, consent grants          federation memberships (FreeParty/FurryParty)
account status, created_at
```

Identifier conventions: `usr_<uuid>` global users, `fed_<uuid>` federations,
`membership(user, federation|application, roles, settings)`. Applications key
their profile rows on the **sub UUID**, never on email (emails change).

Scopes: `openid`, `profile` (name, preferred_username, picture),
`email` (email, email_verified), `tg11.profile` (TG11-specific claims such as
`tg11_username`, `account_state`, `created_at`). Application permissions stay
in each application.

## Reference model (FreeParty is canonical)

FreeParty (`apps/accounts/models.py`) already has the target shape: UUID PK,
unique lower-cased email + username, `state` machine
(active/pending_verification/limited/suspended), verification tokens, account
lifecycle tokens, TOTP device, recovery codes, Django hashers. Shop uses
django-allauth with an email-only integer-PK user. Decision: **FreeParty's model
is the normalised TG11 identity model**; Shop's allauth flows are fine but its
user table needs a UUID identity layer (see migration below).

Flowboard follows the same model in SQLAlchemy (`app/models/user.py`):
UUID `id`, `email`, `username`, `display_name`, `password_hash` in Django's
`pbkdf2_sha256$…` format (so hashes can be moved into the Django-based identity
service without a password reset), `state`, `is_active`, `email_verified_at`,
plus `IdentityLink` (= `ApplicationIdentityLink`) and `UserProfile`.

## Account linking / migration (no silent merges)

`ApplicationIdentityLink` (Flowboard: `identity_links`):

| column | meaning |
|---|---|
| id | UUID |
| user_id | local user |
| provider / issuer | `tg11` / issuer URL |
| subject | TG11 `sub` (global user UUID) |
| email_at_link, username_at_link | snapshot for auditing |
| migration_source | `oidc_login` (auto), `account_link` (user clicked "Link"), `admin` |
| migration_status | linked / pending / revoked |
| linked_at, last_login_at | |

Linking rules implemented in `services/auth.get_or_create_user_for_identity`:

1. existing link for `(provider, sub)` → that user;
2. else a local user with the **same email** is linked **only if the IdP asserts
   `email_verified=true`**; an unverified email never links — the user is told
   to sign in locally and link from *Settings → Security* (explicit
   confirmation);
3. else a new local user is created (SSO-only, no password).

Legacy applications keep their integer ids as legacy metadata; the identity
service keeps a `legacy_links(app, legacy_id, local_uuid, federation_id)` table
populated by migration/link tokens or admin tooling.

## Federation

FurryParty is *Federation 1* with its own database. The identity service
authenticates the person; the FreeParty codebase maps `sub` → membership in
that federation's own DB. Nothing federation-specific lives in the identity
database, and a person can hold memberships in several federations under one
`sub`.

## What the identity provider must implement

Minimum viable `accounts.tg11.org` (a reference implementation ships in
`tg11-accounts/` of this repository — see its README):

* `GET /.well-known/openid-configuration`, `GET /oauth/jwks.json`
* `GET /oauth/authorize` (code + PKCE, `state`, `nonce`, consent screen)
* `POST /oauth/token` (authorization_code, refresh_token; client_secret_basic/post)
* `GET /oauth/userinfo` (Bearer access token)
* `GET /oauth/logout` (RP-initiated logout, `post_logout_redirect_uri`)
* registration, login, email verification, password reset, session list/revoke
* client registry (redirect URIs are exact-match, per client)
* later: TOTP, WebAuthn/passkeys, recovery codes, device management, trusted
  devices, OAuth login providers, admin account-linking tools.

Claims Flowboard consumes: `sub`, `email`, `email_verified`,
`preferred_username`, `name`.

## Flowboard configuration

```
TG11_OIDC_ISSUER=https://accounts.tg11.org
TG11_OIDC_CLIENT_ID=flowboard
TG11_OIDC_CLIENT_SECRET=…
TG11_OIDC_SCOPES=openid profile email
TG11_LOCAL_LOGIN_ENABLED=true    # flip to false once everyone is linked
```

Redirect URI to register: `https://flowboard.fyi/auth/tg11/callback`.
Post-logout redirect: `https://flowboard.fyi/login`.

Transition plan: (1) local login only (now) → (2) both, "Sign in with TG11"
button + link from Settings → (3) TG11 preferred, local password optional →
(4) local login disabled.

## Rolling the other applications in

| app | today | plan |
|---|---|---|
| FreeParty | Django, canonical user model | add OIDC RP (`mozilla-django-oidc` or `django-allauth` OpenID Connect provider config), `IdentityLink` table, link-by-verified-email + explicit link page |
| Shop | Django 6 + allauth, int-PK email user | add `uuid` column (backfill, unique, not null) and `IdentityLink`; configure allauth's OpenID Connect provider pointing at the TG11 issuer; keep orders/addresses keyed on the local user |
| Echoquil | Django default `auth.User` (int PK) | add `accounts` app with `UserIdentity(uuid, user OneToOne)` + `IdentityLink`; adopt FreeParty's state/verification model incrementally; OIDC RP as above; no re-registration |
| FurryParty | FreeParty fork, separate DB | same RP integration as FreeParty; membership table keyed on `sub` |
| CircuitSmith, GridGoblin | new logins | start as OIDC RPs only (no local passwords) |
| Boundless | on hold | documented only |

Shared client library: keep it tiny — an OIDC RP helper (discovery, PKCE, ID
token validation, claims → local user mapping) plus the `IdentityLink` pattern.
The Flowboard implementation (`app/identity/oidc.py`, ~150 lines, only depends
on `httpx` + `joserfc`) is the FastAPI reference; Django apps should use the
maintained `mozilla-django-oidc`/allauth integrations with the same
claim-mapping rules rather than a bespoke shared package.
