# Changelog & Handoff

> **This file is the single source of truth for handoff.** Any agent resuming this work should read
> this file first, then `docs/ARCHITECTURE.md`, then the ADRs.

## Repository state

- **Branch:** `ga4-analytics` — all work lives here. **`main` is untouched** (at `947de7a`,
  "Require an exact same-origin match for state-changing requests").
- **Local clone:** `/tmp/museos` (fresh `--depth 1` clone of `Tooonnnnyyy/museos`).
- **Remote:** `origin = https://github.com/Tooonnnnyyy/museos.git`.
- Revert to the original version at any time with `git checkout main`.

## Done (committed)

### 1. GA4-style analytics dashboard
- `GET /analytics` (page, `web/analytics.html`) + `GET /analytics/summary?days=7|28|90|365`.
- Left rail + date-range switcher, 6 KPI cards with period-over-period deltas, spend/runs SVG chart,
  run funnel (created→planned→executed→reviewed→synthesized), provider & role breakdowns, council score,
  runs table with receipt links, EN/FR/ZH i18n, empty state. No external JS/CSS deps; all dynamic
  strings are HTML-escaped (XSS-safe).
- Backend: `src/museos/analytics.py` (window aggregation; `days` whitelisted; "today" is included).

### 2. Decision Receipt
- `GET /runs/{id}/receipt` (JSON) + `GET /runs/{id}/receipt.md` (Markdown).
- Built from ledger tables only (`agent_runs`, `usage_records`, `policy_decisions`, `events`) — never
  from prompts or keys. `src/museos/receipt.py`.

### 3. Store window queries
- `src/museos/store.py`: `runs_between`, `rows_between`, `events_between` (date-windowed reads).

### 4. Security hardening in this work
- All analytics/receipt output derives from ledger rows; no provider keys anywhere.
- `/analytics` and `/receipt` are read-only GET (no CSRF surface).
- Verified: `.gitignore` covers `.env`, `.env.*`, `providers.toml`, `*.db`, `artifacts/`.

## In progress (uncommitted WIP) — AUTH

Goal: replace the single-user/no-login posture with signup/login (session cookies), plus per-user
data isolation via the existing `workspace_id` column.

**Already written (uncommitted):**
- `src/museos/security.py` — stdlib-only: `hash_password` (scrypt n=16384 r=8 p=1, 32-byte key),
  `verify_password` (constant-time), `new_token` (`secrets.token_hex(32)`), `normalize_email`,
  `valid_email`.
- `src/museos/store.py` — added `UserRow` (`users`), `SessionRow` (`sessions`), a `_aware()` tz helper,
  and CRUD: `create_user` (None on duplicate email), `get_user`, `get_user_by_email`, `create_session`,
  `get_session` (deletes expired), `delete_session`; `list_runs` now takes `workspace_id=`.

**Remaining (auth, in order):**
1. `config.py`: add `auth_enabled: bool` (env `MUSEOS_AUTH_ENABLED`, default **True**) and
   `session_ttl_days: int` (default 30).
2. `api.py`: `POST /auth/signup`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`; a
   `Depends(_require_user)` dependency (401 when auth enabled and no valid session); protect every
   data/mutation endpoint; scope by `workspace_id = f"user:{user.id}"` (or `"default"` when auth off);
   redirect the `/`, `/admin`, `/analytics` HTML routes to `/login` when unauthenticated; add `/login`.
3. `analytics.py`: add a `workspace_id` parameter and filter runs + (via run_id) the other tables.
4. `web/login.html`: signup/login form (apm-style), posts to `/auth/*`, redirects to `/`.
5. Tests: add `tests/test_auth.py`; set `auth_enabled=False` in the existing `Settings(...)` fixtures
   (`tests/test_api_and_adapters.py` `client` fixture, and any other `create_app`/`Settings` calls).
6. Docs: README, SECURITY.md, ARCHITECTURE.md, and a new ADR for auth.

**Auth design decisions (so other agents don't re-litigate):**
- Session = opaque random bearer token in an httpOnly cookie (`museos_session`), stored in `sessions`,
  no signing secret needed, revocable. Cookie `secure=False` for now (served over HTTP / tunnel HTTP
  leg); flip to `secure=True` for production HTTPS.
- Passwords hashed with stdlib scrypt — **no new dependencies** (passlib is unmaintained; avoid it).
- Isolation rides the existing `RunRow.workspace_id`; tasks/usage/agent_runs/policy_decisions/events/
  comparisons are joined through `run_id`.
- CSRF: the existing same-origin middleware already blocks cross-origin non-GET; keep it.
- `auth_enabled` defaults True for the served app, but tests opt out via `Settings(auth_enabled=False)`
  so the 78 existing tests keep passing.

## How to run / verify

```bash
cd /tmp/museos && uv sync
uv run pytest -q            # 78 passed, 1 skipped (postgres) before auth WIP landed
uv run museos serve         # http://127.0.0.1:8099/analytics
```

## Naming (open decision — NOT applied yet)

- Current code/branding still says **MuseOS**.
- User wants a bee/hive/"swarm" name; Chinese is fine, leaning **蜂巢 (honeycomb/hive) multi-agent system**.
- **Collision findings (evidence):** `SwarmOS` collides with `openai/swarm` ★22k (same category) and
  3 existing `SwarmOS` repos; `Hive` collides with `aden-hive` ★11k, Apache Hive, Hive blockchain;
  `Bee` with IBM BeeAI; `Muse` is Meta's agent brand. `HiveOS`/`CombOS`/`NectarOS` not yet checked.
- A rename is mechanical (package dir, imports, `MUSEOS_*` env vars, CLI command, DB filename, HTML
  brand strings, `museos_*` identifiers). Do it as its own commit after auth lands.

## Deployment / preview (transient, do not rely on)

- Outbound ports 7844 (Cloudflare edge) and 22 (SSH) are blocked here; only 443 works.
- Working tunnel: **pinggy** (`ssh -p 443 -R0:localhost:8099 a.pinggy.io`), unauthenticated 60-min URLs.
- Exposed in **demo mode only** (mock providers, no keys). `MUSEOS_ALLOWED_HOSTS` was widened for
  `*.trycloudflare.com`, `*.free.pinggy.net`, `*.run.pinggy-free.link`, `*.a.free.pinggy.link`.
