# Security

SwarmAgentOS is built to run on your own machine.

- **Accounts.** The API now requires signup/login (`/auth/signup`, `/auth/login`), gated by
  `SWARMOS_AUTH_ENABLED` (default on). Sessions are opaque random tokens in httpOnly cookies
  (`SameSite=Lax`), stored in the `sessions` table and revocable by logout. Passwords are stored only
  as scrypt hashes (n=16384, r=8, p=1, per-user random salt), never in plain text. Every run is scoped
  to the signed-in user's `workspace_id`, and every data endpoint filters by it, so one account cannot
  read another account's runs, receipts, analytics or votes.
- **Still local-first.** The server only answers requests addressed to `localhost` / `127.0.0.1` (or the
  hosts in `SWARMOS_ALLOWED_HOSTS`) and caps the budget per run on the server side. To serve other people
  over a network, terminate HTTPS in front of it and set the session cookie `secure=True`; the current
  default is `secure=False` because the app is served over plain HTTP locally.
- **CSRF.** State-changing requests from any other origin are refused by the same-origin middleware;
  session cookies add `SameSite=Lax` on top.
- **Keys.** API keys live in `.env` (owner-only, gitignored) and are referenced by environment-variable
  name. They are never written to `providers.toml`, the database, logs, prompts or API responses.
- **Web tools are read-only and policy-gated.** With `research: true`, research tasks may search the web
  (DuckDuckGo) and fetch pages (byte-capped, http/https only). Hosts resolving to private/loopback/
  link-local/reserved addresses are refused (SSRF guard), every call is a policy decision recorded in
  the ledger, and fetched content is treated as untrusted prompt data. No external writes exist.
- **Subscriptions.** Subscription-backed providers run the vendor's own CLI under your own login. SwarmAgentOS
  never reads or stores the login. See `docs/adr/ADR-005-claude-subscription-adapter.md`.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository ("Security" tab → "Report a
vulnerability"). Do not open a public issue for a security problem.
