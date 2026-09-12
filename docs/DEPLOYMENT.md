# Deployment

## Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # set FLOWBOARD_ENV=development, keys from `python -m app.cli gen-keys`
alembic upgrade head
python -m app.cli create-user --email you@example.com --username you
python -m app.cli import-legacy --file flowboard_tasks.json --user you   # optional
uvicorn app.main:app --reload --port 8000
pytest
```

In development the schema is also auto-created on startup; production relies on
Alembic only.

## Production (198.74.54.235, Ubuntu 22.04, Apache + uvicorn)

Layout: `/var/www/Flowboard` (source), `/var/www/Flowboard/.venv`,
`/var/www/Flowboard/data/flowboard.sqlite3` (database, WAL mode),
`/var/www/Flowboard/.env` (root:flowboard 640), systemd `flowboard.service`
running uvicorn as the `flowboard` system user on `127.0.1.1:8000`, Apache
vhosts `flowboard.fyi.conf` (proxy) and `flowboard.conf` (legacy redirect).

Deploy a new version:

```bash
# from your machine (WSL)
rsync -az --delete --exclude .venv --exclude data --exclude .env --exclude __pycache__ \
      ./ root@198.74.54.235:/var/www/Flowboard/
ssh root@198.74.54.235 'bash /var/www/Flowboard/deploy/deploy.sh'
```

`deploy/deploy.sh` creates the service user, installs requirements into the
venv, runs `alembic upgrade head`, restarts the service and hits `/healthz`.

### Morning digest timer (2.2)

```bash
install -m 644 deploy/systemd/flowboard-digest.service /etc/systemd/system/
install -m 644 deploy/systemd/flowboard-digest.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now flowboard-digest.timer
systemctl list-timers flowboard-digest.timer
journalctl -u flowboard-digest.service -n 50
```

The timer fires every 15 minutes and the command decides who is due (each
user's local hour plus `last_sent_on`), so extra runs are harmless. Digests
need SMTP in `.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
`EMAIL_FROM`); without it the run logs what it would have sent. Remember that
systemd's `EnvironmentFile` does **not** strip inline comments — keep comments
in `.env` on their own lines.

### Database backups (read this before copying anything)

The database runs in **WAL mode**, so `cp data/flowboard.sqlite3` is not a
backup: recent commits live in `data/flowboard.sqlite3-wal` until a checkpoint,
and a copy of the main file alone can be far behind the live database (on
2026-09-12 the main file was a whole migration behind while the service was
serving fine). Use the online-backup command, which snapshots the live database
WAL included, verifies it and writes a SHA-256 beside it:

```bash
sudo -u flowboard /var/www/Flowboard/.venv/bin/python -m app.cli backup            # data/flowboard.sqlite3.bak-v<version>-<ts>
sudo -u flowboard /var/www/Flowboard/.venv/bin/python -m app.cli backup --to /root/backups/flowboard/$(date +%Y%m%d-%H%M%S)/flowboard.sqlite3 --keep 14
sha256sum -c data/flowboard.sqlite3.bak-*.sha256
```

Run it **before every migration**. If you must copy files by hand, copy
`flowboard.sqlite3`, `-wal` and `-shm` together, or stop the service first.

Postgres can be used instead by setting
`FLOWBOARD_DATABASE_URL=postgresql+psycopg://…` and installing `psycopg` (then
back it up with `pg_dump`, not this command).

## Cloudflare

The zone proxies to the origin. TLS at the origin uses a Cloudflare Origin CA
cert in `/etc/apache2/ssl/flowboard.fyi.{pem,key}` (valid until 2041); the
zone's SSL mode must be **Full (strict)**. Real client IPs arrive in
`CF-Connecting-IP`; Apache's `X-Forwarded-*` headers are trusted by uvicorn
(`--forwarded-allow-ips=127.0.0.1`).

## Health & logs

* `curl https://flowboard.fyi/healthz`
* `journalctl -u flowboard -n 100`
* `/var/log/apache2/flowboard_fyi_{access,error}.log`
