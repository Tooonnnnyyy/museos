# Changelog & Handoff

> **This file is the single source of truth for handoff.** Any agent resuming this work should read
> this file first, then `docs/ARCHITECTURE.md`, then the ADRs.

## Repository state

- **Branch:** `ga4-analytics` — all work lives here. **`main` is untouched** (at `947de7a`,
  "Require an exact same-origin match for state-changing requests").
- **Local clone:** `/tmp/swarmos` (fresh `--depth 1` clone of `Tooonnnnyyy/museos`).
- **Remote:** `origin = https://github.com/Tooonnnnyyy/museos.git`.
- Revert to the original version at any time with `git checkout main`.

## Done (committed)

### 1. GA4-style analytics dashboard
- `GET /analytics` (page, `web/analytics.html`) + `GET /analytics/summary?days=7|28|90|365`.
- Left rail + date-range switcher, 6 KPI cards with period-over-period deltas, spend/runs SVG chart,
  run funnel (created→planned→executed→reviewed→synthesized), provider & role breakdowns, council score,
  runs table with receipt links, EN/FR/ZH i18n, empty state. No external JS/CSS deps; all dynamic
  strings are HTML-escaped (XSS-safe).
- Backend: `src/swarmos/analytics.py` (window aggregation; `days` whitelisted; "today" is included).

### 2. Decision Receipt
- `GET /runs/{id}/receipt` (JSON) + `GET /runs/{id}/receipt.md` (Markdown).
- Built from ledger tables only (`agent_runs`, `usage_records`, `policy_decisions`, `events`) — never
  from prompts or keys. `src/swarmos/receipt.py`.

### 3. Store window queries
- `src/swarmos/store.py`: `runs_between`, `rows_between`, `events_between` (date-windowed reads).

### 4. Security hardening in this work
- All analytics/receipt output derives from ledger rows; no provider keys anywhere.
- `/analytics` and `/receipt` are read-only GET (no CSRF surface).
- Verified: `.gitignore` covers `.env`, `.env.*`, `providers.toml`, `*.db`, `artifacts/`.

## Done (committed) — AUTH + RENAME

### 5. Authentication (signup/login) — DONE
- `src/swarmos/security.py` — stdlib-only: `hash_password` (scrypt n=16384 r=8 p=1, 32-byte key),
  `verify_password` (constant-time), `new_token`, `normalize_email`, `valid_email`.
- `src/swarmos/store.py` — `UserRow` (`users`), `SessionRow` (`sessions`) + CRUD; `list_runs` and
  `runs_between` take `workspace_id=`.
- `src/swarmos/config.py` — `auth_enabled` (env `SWARMOS_AUTH_ENABLED`, default True),
  `session_ttl_days` (30).
- `src/swarmos/api.py` — `POST /auth/signup|login|logout`, `GET /auth/me`, `_require_user` dependency
  (passes through as anonymous when auth is off); every data/mutation endpoint protected and scoped to
  `workspace_id = user:<id>`; `/`, `/admin`, `/analytics` redirect to `/login` when unauthenticated.
- `src/swarmos/web/login.html` — signup/sign-in page, EN/FR/ZH.
- `docs/adr/ADR-006-authentication.md`, SECURITY.md, README, `.env.example` updated.
- `tests/test_auth.py` (4 tests: 401/redirect, login lifecycle, validation, per-user isolation);
  existing API tests set `Settings(auth_enabled=False)`.

### 6. Rename MuseOS → SwarmAgentOS (蜂巢) — DONE
- Package/dir `src/museos` → `src/swarmos`; all imports, `pyproject` name + `swarmos` CLI script,
  `SWARMOS_*` env vars, `swarmos.db` default, `swarmos_session` cookie, `swarmos_*` identifiers,
  HTML brand strings. **GitHub repo URL stays `Tooonnnnyyy/museos`** (repo rename deferred by user).

## Done (committed) — WEB TOOLS + PR REVIEW + WORKFLOW

### 7. Read-only web tools (research)
- `src/swarmos/tools.py`: `web_search` (DuckDuckGo, key-less) + `web_fetch` (byte-capped, SSRF guard,
  stdlib HTML→text). No tool-calling loop: research tasks are enriched before their one model call
  (1 search + ≤3 fetches appended as "Verified web sources").
- Every call is policy-gated (`tool.<name>` vs the task's `tools_allowed`), recorded in a new
  `tool_runs` table and `tool.used` events; costs $0 but consumes a budget step.
- Switches: `SWARMOS_TOOLS_ENABLED` (server, default on) + `research: true` per run (UI checkbox,
  `--research` flag). Receipts gain a "Web tools" section; analytics gains `tool_calls`.
- SSRF: private/loopback/link-local/reserved hosts refused at resolution time.
- ADR-007 documents the design.

### 8. PR review swarm (`swarmos pr-review`)
- `swarmos pr-review <owner/repo> <n> [--post] [--research]` fetches title/body/diff via the user's
  `gh` CLI, runs the swarm (cross-review on), writes `artifacts/pr-<repo>-<n>-review.md`, and posts
  the review as a PR comment with `--post`. No GitHub credentials stored.

### 9. WORKFLOW.md
- Task book for running parallel coding agents (kitty + tmux + worktrees + gh), written for humans
  and agents alike.

## How to run / verify

```bash
cd /tmp/museos && uv sync
uv run pytest -q            # 82 passed, 1 skipped (postgres)
uv run swarmos serve        # http://127.0.0.1:8099/analytics  (sign up at /login)
```

## Naming (state)

- Brand: **SwarmAgentOS** (EN) / **蜂巢** (ZH), code identifier `swarmos`. Applied.
- GitHub repo rename (`Tooonnnnyyy/museos` → `Tooonnnnyyy/swarmos`) NOT done — user chose code-only
  for now; it changes the repo URL (old links 404).
- Collisions documented: `SwarmOS` vs `openai/swarm` ★22k (same category); `Hive` vs `aden-hive` ★11k /
  Apache Hive / Hive mining OS; `Bee` vs IBM BeeAI; `Muse` vs Meta's agent brand.

## Deployment / preview (transient, do not rely on)

- Outbound ports 7844 (Cloudflare edge) and 22 (SSH) are blocked here; only 443 works.
- Working tunnel: **pinggy** (`ssh -p 443 -R0:localhost:8099 a.pinggy.io`), unauthenticated 60-min URLs.
- Exposed in **demo mode only** (mock providers, no keys). `SWARMOS_ALLOWED_HOSTS` was widened for
  `*.trycloudflare.com`, `*.free.pinggy.net`, `*.run.pinggy-free.link`, `*.a.free.pinggy.link`.
