# Domain migration: flowboard.vulpfin.com → flowboard.fyi (2026-09-12)

## Before

* Apache vhost `flowboard.conf`: `flowboard.vulpfin.com` :80 → 301 to https;
  :443 with the `*.vulpfin.com` Cloudflare origin cert, proxied to uvicorn on
  `127.0.1.1:8000`, plus `Alias /download/flowboard.vfa`.
* `flowboard.fyi` DNS already at Cloudflare, but no vhost → requests fell
  through to the default vhost (bettercorporatelogowear.org, a Django app)
  which answered **400 Bad Request** (DisallowedHost).
* No TLS cert for the new name; the file supplied as `flowboard.fyi.pem` was a
  Cloudflare *client* certificate (EKU: TLS Web Client Authentication). A
  proper Cloudflare Origin CA cert was supplied later and is now in use.

## After

| host | :80 | :443 |
|---|---|---|
| flowboard.fyi | 301 → https (ACME challenge path excluded) | app (uvicorn 127.0.1.1:8000), static via Apache, `/download/flowboard.vfa` kept, HSTS |
| www.flowboard.fyi | 301 → https://flowboard.fyi | 301 → apex (no DNS record yet) |
| flowboard.vulpfin.com | 301 → `https://flowboard.fyi{path}?{query}` | same, with the vulpfin.com cert |

Verified from the public internet:
`https://flowboard.vulpfin.com/boards/work/tasks/123?view=details` →
`301 https://flowboard.fyi/boards/work/tasks/123?view=details`;
`http://flowboard.fyi/x?y=1` → `301 https://flowboard.fyi/x?y=1`;
`https://flowboard.fyi/login` 200 (HSTS, nosniff, frame-options present);
`/static/styles.css` 200 `text/css`; `/download/flowboard.vfa` 200.

Certificates: Cloudflare Origin CA (`/etc/apache2/ssl/flowboard.fyi.pem`,
valid to 2041) is active; a Let's Encrypt certificate for `flowboard.fyi` is
also issued and auto-renews (`/etc/letsencrypt/live/flowboard.fyi/`, webroot
`/var/www/Flowboard/acme`). Cloudflare zone SSL mode should be **Full (strict)**
(operator action).

Application configuration updated (`/var/www/Flowboard/.env`):
`FLOWBOARD_SITE_URL=https://flowboard.fyi`,
`FLOWBOARD_ALLOWED_HOSTS=flowboard.fyi,www.flowboard.fyi,flowboard.vulpfin.com,…`,
`FLOWBOARD_TRUSTED_ORIGINS=https://flowboard.fyi,https://flowboard.vulpfin.com`.
Absolute URLs (OAuth callbacks, feed URLs, emails) derive from
`FLOWBOARD_SITE_URL`. A search of the 2.0 tree for `flowboard.vulpfin.com`
finds only the allow-lists and the redirect vhost.

Files: `deploy/apache/flowboard.fyi.conf`, `deploy/apache/flowboard.conf`
(redirect), backups of the originals in
`/root/backups/flowboard/20260912-100659/apache/`.
