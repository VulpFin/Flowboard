# Rollback & disaster recovery

Recovery point: **2026-09-12 10:06:59 UTC** (backup id `20260912-100659`),
taken before any production change. Flowboard 1.x had no git repository; the
pre-upgrade source is the tarball in the backup. Flowboard 2.0 code is now in
git (`U:\Projects\Vulpfin Flowboard`, first commit `cbd96cf`, deployed
`86b4189`+); TG11 Accounts in `U:\Projects\TG11Accounts` (`f4aa54f`).

## Backup locations

| what | server | local (Windows) |
|---|---|---|
| Flowboard pre-upgrade (source+data tarball, `flowboard_tasks.json`, `.env`, Apache vhosts, systemd unit, origin cert, pip freeze, MANIFEST, SHA256SUMS) | `/root/backups/flowboard/20260912-100659/` | `U:\Projects\flowboard-backups\20260912-100659\server\` |
| Local project archive (pre-upgrade working tree incl. `.env`) | – | `U:\Projects\flowboard-backups\20260912-100659\local\flowboard-local-pre-upgrade-20260912-100659.tar.gz` |
| FreeParty (vhosts, both `.env`s, systemd unit, git state, **pg_dump of the live DB**) | `/root/backups/freeparty/20260912-100659/` | `U:\Projects\flowboard-backups\20260912-100659\freeparty\` |
| FurryParty / Federation 1 (`.env`, git commit, pg_dump of `freeparty-dev_postgres_data`) | `/root/backups/furryparty/20260912-100659/` | `U:\Projects\flowboard-backups\20260912-100659\furryparty\` |
| Post-deploy snapshot (Flowboard 2.0 sqlite, TG11 Accounts sqlite, new vhosts/units/env) | `/root/backups/flowboard/20260912-post-deploy/` | `U:\Projects\flowboard-backups\20260912-100659\post-deploy\` |

All server copies were verified against `SHA256SUMS` after rsync
(`sha256sum -c` → OK). Local copies are `chmod go-rwx`.

## Roll Flowboard back to 1.x (full)

```bash
systemctl stop flowboard
mv /var/www/Flowboard /var/www/Flowboard.v2                     # keep 2.0 for forensics
mkdir /var/www/Flowboard && cd /var/www/Flowboard
tar xzf /root/backups/flowboard/20260912-100659/flowboard-source-and-data.tar.gz
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # or: cp -a /var/www/Flowboard.v2/legacy/../.venv (venv was reused; 1.x deps are older)
cp /root/backups/flowboard/20260912-100659/systemd/flowboard.service /etc/systemd/system/flowboard.service
cp /root/backups/flowboard/20260912-100659/apache/flowboard.conf /etc/apache2/sites-available/flowboard.conf
a2dissite flowboard.fyi.conf            # optional: 1.x knows nothing about the new domain
apachectl configtest && systemctl daemon-reload && systemctl restart flowboard && systemctl reload apache2
```

The 1.x data file `flowboard_tasks.json` is still in place (2.0 only *reads*
it during import), so tasks are intact. The 2.0 SQLite database
(`/var/www/Flowboard/data/flowboard.sqlite3`) can be kept for later.

## Roll back only the domain change (keep 2.0)

```bash
cp /root/backups/flowboard/20260912-100659/apache/flowboard.conf /etc/apache2/sites-available/flowboard.conf   # proxy vhost for flowboard.vulpfin.com
a2dissite flowboard.fyi.conf
sed -i 's#^FLOWBOARD_SITE_URL=.*#FLOWBOARD_SITE_URL=https://flowboard.vulpfin.com#' /var/www/Flowboard/.env
apachectl configtest && systemctl reload apache2 && systemctl restart flowboard
```

## Restore the Flowboard 2.0 database

SQLite: stop the service, copy `flowboard.sqlite3` from the post-deploy
snapshot over `/var/www/Flowboard/data/flowboard.sqlite3` (remove `-wal`/`-shm`
files), `chown flowboard:flowboard`, start. Alembic migrations: `0001` is the
initial schema; there is nothing to downgrade to — a downgrade is "restore the
pre-migration file" (there was none: 1.x had no database).

## Restore FreeParty / FurryParty databases

```bash
docker exec -i freeparty-db-1     pg_restore -U freeparty -d freeparty --clean --if-exists < /root/backups/freeparty/20260912-100659/freeparty-db.pgdump
docker exec -i freeparty-dev-db-1 pg_restore -U freeparty -d freeparty --clean --if-exists < /root/backups/furryparty/20260912-100659/furryparty-db.pgdump
```

## Undo the freeparty.dev fix

```bash
cp /root/backups/freeparty/20260912-100659/apache/freeparty.dev.conf /etc/apache2/sites-available/freeparty.dev.conf
cp /var/www/Freeparty/.env.bak.20260912 /var/www/Freeparty/.env
docker compose -f /var/www/Freeparty/compose.yaml -p freeparty up -d --no-deps web
apachectl configtest && systemctl reload apache2
```

## Undo FurryParty (Federation 1)

```bash
systemctl disable --now furryparty; a2dissite furryparty.xyz.conf; systemctl reload apache2
cd /var/www/Freeparty.dev && git checkout 46303ad && cp /root/backups/furryparty/20260912-100659/Freeparty.dev.env .env
```

## Undo TG11 SSO in Flowboard (keep local login)

Blank `TG11_OIDC_ISSUER` in `/var/www/Flowboard/.env` and `systemctl restart
flowboard` — the "Sign in with TG11" button disappears, local passwords keep
working, identity links are retained for later.

## Remove the TG11 Accounts service

`systemctl disable --now tg11-accounts; a2dissite accounts.tg11.org.conf;
systemctl reload apache2`. Data stays in `/var/www/TG11Accounts/data`.

## Certificates / certbot

The system `certbot` is broken (pyOpenSSL ↔ cryptography mismatch in the
system Python). Renewals now run from `/opt/certbot-venv/bin/certbot` via a
systemd drop-in (`/etc/systemd/system/certbot.service.d/override.conf`). To
undo: delete the drop-in and `systemctl daemon-reload`. Certificates:
`/etc/letsencrypt/live/{flowboard.fyi,furryparty.xyz}/`; flowboard.fyi
currently serves the Cloudflare origin cert `/etc/apache2/ssl/flowboard.fyi.*`.

## Service/restart cheat-sheet

```
systemctl restart flowboard | tg11-accounts | furryparty | freeparty
journalctl -u flowboard -n 100
apachectl configtest && systemctl reload apache2
```
