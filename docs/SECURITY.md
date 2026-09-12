# Security notes

* **Sessions**: server-side `user_sessions` rows; the cookie holds only a
  signed session id (HttpOnly, Secure, SameSite=Lax). Password changes revoke
  other sessions; users can list/revoke sessions in Settings → Security.
* **CSRF**: every state-changing request must carry the session's CSRF token
  (hidden form field or `X-CSRF-Token` header — HTMX sends it automatically via
  `hx-headers` on `<body>`); anonymous forms use a double-submit cookie. The
  `Origin` header is additionally checked against `FLOWBOARD_TRUSTED_ORIGINS`.
* **Passwords**: Django-compatible PBKDF2-SHA256 (870k iterations), minimum 10
  characters, constant-time compare, timing-equalised login for unknown users.
* **Object-level authorization**: boards via memberships, tasks via boards,
  credentials/connections/links via the owning user — never by bare id. See
  `tests/test_boards_isolation.py`.
* **Secrets at rest**: AES-256-GCM with per-owner AAD (see docs/AI_PROVIDERS.md).
  Keys are never returned to the browser; UI shows a masked hint only.
* **Provider calls**: strictly server-side; adapters only ever receive the
  current user's decrypted secrets for the current request.
* **Feeds**: 256-bit random tokens, stored hashed, rotatable/revocable; URLs
  carry no identifiers.
* **Headers**: nosniff, frame-options, referrer-policy, permissions-policy at
  the app; HSTS at Apache.
* **Host checking**: `TrustedHostMiddleware` with `FLOWBOARD_ALLOWED_HOSTS`.
* **Logging**: usage records contain provider/model/tokens/latency/error kind;
  prompt bodies only when explicitly enabled; API keys never.
* **Process**: uvicorn runs as an unprivileged `flowboard` user with systemd
  hardening (`NoNewPrivileges`, `ProtectSystem=full`, `PrivateTmp`).
* **Invitations**: 256-bit tokens stored hashed, 14-day expiry, single use, and
  redeemable only by the address they were sent to; re-inviting rotates the
  token. Only board owners/admins may invite, change roles or remove members.
* **Support form** (`/support`, open to signed-out visitors by design): CSRF +
  honeypot, message and header values length-capped and stripped of CR/LF (no
  header injection), the reporter's address goes in `Reply-To` and never in
  `From`, and an hourly cap per user / per client IP
  (`FLOWBOARD_SUPPORT_MAX_PER_HOUR`). Diagnostics are taken from what the
  server knows (version, path, user agent), not from form input.
