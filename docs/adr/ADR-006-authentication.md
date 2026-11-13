# ADR-006: Authentication and per-user isolation

**Status:** Accepted · 2026-11-13

## Context

Phase 1 shipped with no login (SECURITY.md documented single-user localhost use). Analytics and the
Decision Receipt exposed a richer view of the ledger, and any public exposure (even a preview tunnel)
made anonymous access unacceptable. Signup/login was the next step of ADR-004's deferred list.

## Decision

- **Accounts**: `users` table (id, email unique, scrypt password hash). `sessions` table (opaque 256-bit
  random token, user_id, expires_at). No JWT, no signing secret to manage.
- **Password hashing**: stdlib `hashlib.scrypt` (n=2^14, r=8, p=1, 32-byte key, per-user 16-byte salt),
  constant-time compare. No new dependency (passlib is unmaintained).
- **Transport**: session token in an httpOnly `SameSite=Lax` cookie, `secure=False` for now because the
  app is served over plain HTTP locally; flip to `secure=True` once HTTPS terminates at this app. Logout
  deletes the row, so sessions are revocable.
- **Gate**: `SWARMOS_AUTH_ENABLED` (default on). When off, `_require_user` passes through as anonymous and
  the workspace stays `"default"`, preserving the CLI and the old single-user behavior.
- **Isolation**: an authenticated user's workspace is `user:<id>`. `POST /runs` forces it on the request;
  every data endpoint filters by it. Runs are filtered in SQL; tasks/usage/agent_runs/policy_decisions/
  events/comparisons join through `run_id`.
- **UI**: `/login` (signup + sign-in, EN/FR/ZH); `/`, `/admin`, `/analytics` redirect to it when
  unauthenticated; data endpoints return 401 (JSON). `/health` stays public (liveness only).
- **CSRF**: the existing same-origin middleware still refuses cross-origin non-GET requests; the cookie's
  `SameSite=Lax` is a second line.

## Consequences

- The database schema grew two tables (`users`, `sessions`) via the same `create_all` path; migrations
  are still a tracked follow-up (ADR-004).
- Serving other people over a network still requires HTTPS termination + `secure=True` + a real host
  allowlist; this ADR removes the "no login" blocker, not the "localhost only" posture.
- Tests opt out with `Settings(auth_enabled=False)`; `tests/test_auth.py` covers the enabled path.
