# Vulpfin Flowboard 2.x

A user-owned planning environment: multiple boards per account, a context-aware
planner, an AI assistant that runs on **your own** provider keys, calendar
export/sync, and TG11 single sign-on readiness.

* Production: https://flowboard.fyi (old `flowboard.vulpfin.com` redirects)
* Docs: [`docs/`](docs/) — DEPLOYMENT, ENVIRONMENT, AI_PROVIDERS, CALENDARS, ECOSYSTEM_INCIDENTS,
  BOARDS, TG11_SSO, SECURITY, DOMAIN_MIGRATION, ROLLBACK
* Licence: AGPL-3.0-or-later · © 2025-2026 TG11

```
app/            FastAPI application (models, services, ai, calendars, identity, web)
migrations/     Alembic migrations
deploy/         Apache vhosts, systemd unit, deploy script
tests/          pytest suite (isolation, crypto, providers, calendars, migration, pages)
legacy/         Flowboard 1.x sources kept for reference (not imported)
```
