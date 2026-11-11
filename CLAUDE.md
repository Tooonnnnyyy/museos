# MuseOS — context for coding agents

Read this first. Keep it current: when a decision or constraint changes, update this file in the same change.

## What this is

A local multi-model orchestration engine. One idea in → Chief checks the goal → Chief plans a task graph →
tasks run in parallel on different model providers → one bounded cross-review round → one merged answer that
lists agreements, disagreements, assumptions, risks, evidence and limitations. Users bring their own models:
API keys, any OpenAI-compatible endpoint, or a subscription through a command-line tool.

## Where things are

| File | Holds |
|---|---|
| `README.md` | What it is, quick start, usage, configuration. Usage guides: `README.zh-CN.md`, `README.fr.md` |
| `docs/ARCHITECTURE.md` | How a run flows through the code, data model, known limitations |
| `docs/adr/` | Decisions: 001 gateway, 002 orchestration, 003 no queue yet, 004 database, 005 subscriptions |
| `CONTRIBUTING.md`, `SECURITY.md` | Contribution terms and security model |

## Rules that are easy to break

- The goal is convergence, not more agents. Reject scope growth that does not serve it.
- Role (what work) and provider (which model) stay separate. The router picks the provider per task.
- Every model call goes through `engine.call_model`; never call an adapter directly.
- Secrets: only environment-variable names in config (`api_key_env`). Never put keys in TOML, prompts,
  database rows, logs or chat. `.env`, `providers.toml`, `*.db` and `artifacts/` are gitignored.
- Do not ship model IDs or prices that were not checked in the provider's own docs. Users type model IDs.
- Subscription providers (`claude_cli`, `command`) are personal, local and low-volume: `max_concurrency = 1`
  and the call caps stay. Read ADR-005 before touching them. Never present them as a way to serve others.
- Agents have no tools and MuseOS performs no external writes. Any future one must pass
  `policy.PolicyEngine` and the kill switch.
- Do not claim something works unless it was run. Mocks in tests; live calls only with a small budget.
- Reasoning models spend output tokens on thinking: keep `max_output_tokens` at 4000 or more.
- Three languages: every user-facing string exists in `en`, `fr` and `zh` (page: `I18N` in
  `web/index.html`; server: `i18n.py`). Model output language comes from `RunRequest.language`.
- The page follows a fixed visual system: flat color bands, 2px ink rules, square corners, pill buttons with
  solid offset shadows, Manrope and DM Mono. No gradients, rounded cards or soft shadows.
- The Command Center (`/`) and the admin page (`/admin`) both stay. Admin is read-only and never returns a
  secret, a connection string or a key value.
- A blind test must stay blind: no provider names, per-agent sections or progress before the vote.
- Run one `pytest` process at a time.

## Commands

```bash
uv sync
uv run museos setup        # connect a model
uv run museos providers    # what is configured
uv run museos serve        # web page at http://127.0.0.1:8000
uv run museos run "idea" --clarify --cross-review --budget 0.10
uv run pytest -q
```
