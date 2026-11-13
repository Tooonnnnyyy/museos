# Architecture

## How a run flows

```
idea
 └─ clarify (optional)   Chief classifies the request. A simple question or an action request is answered
 │                       directly and the run ends. A project: Chief restates the goal and asks up to 3 questions
     └─ plan             Chief returns a task graph (validated: no cycles, known roles)
         └─ execute      tasks with no unmet dependencies run in parallel, each on a routed provider
             └─ review   (optional) each result is challenged once by a different provider
                 └─ synthesize   one merged answer + agreements, disagreements, limitations
```

A blind test adds one more call beside the run: a single model answers the same brief alone.

## Modules

| Module | Role |
|---|---|
| `contracts.py` | Typed request, plan, task, result and deliverable models |
| `gateway/` | Provider adapters behind one interface: `litellm` (API keys, OpenAI-compatible endpoints), `claude_cli` and `command` (subscription or local command-line tools), `mock`; registry with a circuit breaker and process-wide concurrency limits |
| `router.py` | Picks a provider per task by capability, load, cost and health; returns the reason and fallbacks |
| `policy.py` | Deterministic checks before every call: budget, step ceiling, allow-lists, kill switch |
| `engine.py` | `call_model`, the only path to a model: policy → reserve budget → timeout → retry → fallback → validate → audit |
| `chief.py` | Planning call with schema validation and a template plan if planning fails |
| `scheduler.py` | Runs the task graph with a concurrency limit and cancellation |
| `synthesis.py` | Cross-review, synthesis and Markdown rendering |
| `orchestrator.py` | Run lifecycle (LangGraph): clarify, answer, plan, execute, review, synthesize, baseline |
| `blindtest.py` | Same-shape, name-scrubbed view of both answers; vote tally |
| `i18n.py` | Output-language rule for prompts; system notes in English, French, Chinese |
| `admin.py`, `web/admin.html` | Read-only usage, cost, health and settings; no secrets |
| `setup.py` | `swarmos setup`: add a provider, store its key in `.env` |
| `store.py` | SQLAlchemy models and persistence (SQLite by default, PostgreSQL optional) |
| `analytics.py` | GA4-style aggregate numbers over a date window; derives everything from ledger rows |
| `receipt.py` | Decision Receipt for one run, in JSON and Markdown; built from the ledger, never from a key |
| `tools.py` | Read-only web tools (search/fetch) with SSRF guard and plain-text extraction |
| `cli.py` (`pr-review`) | Fetch a PR via `gh`, run the swarm on it, write the review to `artifacts/`, optionally post it |
| `api.py`, `cli.py`, `web/index.html` | HTTP API, command line, and the Command Center page |

## What is stored

`runs`, `tasks`, `agent_runs` (every model attempt), `usage_records`, `policy_decisions`, `events`,
`comparisons` (blind tests). Keys are never stored.

## Guarantees the tests cover

Independent tasks overlap in time; a failed or slow provider is retried, then replaced, and finished work is
kept; budget and step ceilings hold under concurrency; a cancel keeps finished results; duplicate
submissions with the same `Idempotency-Key` create one run; simultaneous answers start a run once; a blind
test reveals nothing before the vote; secrets do not reach prompts, the database or API responses.

## Known limitations

- **A restart loses runs in progress.** Run state between steps is held in memory. Finished runs and runs
  waiting for your answers are safe.
- **No login and no multi-user separation.** Local, single-user use only. See `SECURITY.md`.
- **Agents have no tools.** They cannot browse, search or act; answers are reasoning without sources, and the
  result says so.
- **No queue or workers.** Runs execute inside the server process.
- **Schema changes are not migrated.** Tables are created on first start; there are no migration scripts yet.
- **Whether a multi-model answer beats one strong model depends on your questions.** That is why the blind
  test is built in. Treat the multi-agent pipeline as a hypothesis until your own votes say otherwise.
