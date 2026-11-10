# Security

MuseOS is built to run on your own machine.

- **No login.** The server only answers requests addressed to `localhost` / `127.0.0.1` and caps the budget
  per run on the server side. Do not expose the port to a network or the internet. If you need remote
  access, put it behind your own authenticated proxy.
- **Keys.** API keys live in `.env` (owner-only, gitignored) and are referenced by environment-variable
  name. They are never written to `providers.toml`, the database, logs, prompts or API responses.
- **Model output is untrusted.** Agents have no tools and cannot take actions. Text from users and from
  other agents is delimited in prompts and treated as data.
- **Subscriptions.** Subscription-backed providers run the vendor's own CLI under your own login. MuseOS
  never reads or stores the login. See `docs/adr/ADR-005-claude-subscription-adapter.md`.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository ("Security" tab → "Report a
vulnerability"). Do not open a public issue for a security problem.
