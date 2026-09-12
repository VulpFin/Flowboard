#!/usr/bin/env bash
# Server-side deploy for Flowboard 2.x.  Run as root on the production host
# after the source tree has been synced to /var/www/Flowboard (rsync/git).
#
#   bash deploy/deploy.sh            # install deps, migrate, restart, health check
#   bash deploy/deploy.sh --no-restart
set -euo pipefail
APP=/var/www/Flowboard
PY=${PY:-python3}
cd "$APP"

if ! id flowboard >/dev/null 2>&1; then
  useradd --system --home "$APP" --shell /usr/sbin/nologin flowboard
fi
mkdir -p "$APP/data"
chown -R flowboard:flowboard "$APP/data"
chmod 750 "$APP/data"
[ -f "$APP/.env" ] && { chown root:flowboard "$APP/.env"; chmod 640 "$APP/.env"; }

if [ ! -x "$APP/.venv/bin/python" ]; then
  $PY -m venv "$APP/.venv"
fi
"$APP/.venv/bin/pip" install --quiet --upgrade pip
"$APP/.venv/bin/pip" install --quiet -r "$APP/requirements.txt"

# database migrations (idempotent)
sudo -u flowboard "$APP/.venv/bin/alembic" -c "$APP/alembic.ini" upgrade head
sudo -u flowboard "$APP/.venv/bin/python" -m app.cli check

if [ "${1:-}" != "--no-restart" ]; then
  systemctl daemon-reload
  systemctl restart flowboard
  sleep 2
  systemctl --no-pager --lines=5 status flowboard || true
  curl -fsS http://127.0.1.1:8000/healthz && echo
fi
