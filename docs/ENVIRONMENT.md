# Environment variables

All configuration is read from the environment (or `/var/www/Flowboard/.env`).
See `.env.example` for a complete annotated template.

| Variable | Required | Purpose |
|---|---|---|
| `FLOWBOARD_ENV` | yes | `production` / `development` / `test` (dev/test relax cookie security, auto-create schema, enable `/api/docs`) |
| `FLOWBOARD_SECRET_KEY` | yes | signs session cookies and OAuth/OIDC flow cookies |
| `FLOWBOARD_CREDENTIAL_ENCRYPTION_KEY` | yes (prod) | 32-byte base64/hex key for AES-256-GCM encryption of AI keys and calendar tokens |
| `FLOWBOARD_CREDENTIAL_ENCRYPTION_KEY_V<n>` | no | additional key versions for rotation |
| `FLOWBOARD_SITE_URL` | yes | canonical URL used for absolute links, OAuth callbacks, feed URLs |
| `FLOWBOARD_SITE_NAME` | no | display name |
| `FLOWBOARD_ALLOWED_HOSTS` | yes (prod) | Host header allow-list (TrustedHostMiddleware) |
| `FLOWBOARD_TRUSTED_ORIGINS` | yes (prod) | Origin header allow-list for state-changing requests (CSRF layer 2) |
| `FLOWBOARD_DATABASE_URL` | no | SQLAlchemy URL; default SQLite in the data dir |
| `FLOWBOARD_DATA_DIR` | no | data directory (SQLite file) |
| `FLOWBOARD_ALLOW_REGISTRATION` | no | `false` = invite only (create users via CLI) |
| `FLOWBOARD_COOKIE_SECURE` | no | Secure flag on cookies (forced off in dev/test) |
| `FLOWBOARD_SESSION_MAX_AGE` | no | seconds |
| `FLOWBOARD_AI_LOG_PROMPTS` | no | store prompt/response text in usage records |
| `FLOWBOARD_PROXY_HEADERS` | no | honour X-Forwarded-* from Apache |
| `SMTP_HOST/PORT/USER/PASSWORD/USE_TLS`, `EMAIL_FROM` | for password reset, invitations, digests and support | outgoing mail |
| `FLOWBOARD_SUPPORT_EMAIL` | no | where the footer's *Contact support* form sends; defaults to the `EMAIL_FROM` address |
| `FLOWBOARD_SUPPORT_MAX_PER_HOUR` | no | support reports per user (or per client IP when signed out), default 5 |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | for Google Calendar | OAuth web client |
| `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MICROSOFT_TENANT_ID` | for Microsoft 365 | Entra app registration |
| `TG11_OIDC_ISSUER`, `TG11_OIDC_CLIENT_ID`, `TG11_OIDC_CLIENT_SECRET`, `TG11_OIDC_SCOPES`, `TG11_OIDC_LOGIN_LABEL` | for TG11 SSO | OIDC relying-party settings |
| `TG11_LOCAL_LOGIN_ENABLED` | no | disable local password login once SSO is universal |

Generate the two keys with `python -m app.cli gen-keys`. Never commit `.env`.
