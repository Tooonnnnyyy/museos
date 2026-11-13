# SwarmAgentOS

English · [简体中文](README.zh-CN.md) · [Français](README.fr.md)

> One idea in. Several AI models at work. One merged answer out.

You have an idea and several AI models you could ask. Today that means pasting the same thing into each
app, reading every long answer, and merging them yourself. SwarmAgentOS does that loop for you, on your own
machine, with **your own models**:

1. **Chief checks the goal.** It restates your idea and asks up to three questions if it is vague.
2. **Chief splits the work** into a few tasks and gives each to a suitable model. Tasks run in parallel.
3. **Models review each other** (optional), each result challenged by a different model.
4. **One answer comes back**, with agreements, disagreements, next steps, assumptions, risks and limits.

It also includes a **blind test**: one single model answers the same question alone, both answers are shown
unsigned, and you vote. After ten votes you know whether the multi-model route is worth it *for your
questions*, instead of taking anyone's word for it.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). No database to install.

```bash
git clone https://github.com/Tooonnnnyyy/museos.git && cd museos
```

```bash
uv sync
```

```bash
uv run swarmos serve
```

Open http://127.0.0.1:8000 . Without any configuration SwarmAgentOS runs in **demo mode** with built-in mock
models, so you can click through everything; the answers are placeholder text.

## Connect your own models

```bash
uv run swarmos setup
```

Run it once per model. It asks which kind, reads the key with hidden input, checks it where the provider
allows, and writes `providers.toml` plus `.env`. Two or more *different* models work best.

| You have | Choose in setup | Notes |
|---|---|---|
| An API key from OpenAI, Anthropic, Google, DeepSeek or xAI | that provider | Billed per use by the provider |
| An OpenRouter key | OpenRouter | One key reaches many vendors' models |
| Qwen, Kimi, GLM, a company gateway, a local Ollama… | Any OpenAI-compatible endpoint | You give the base URL and model ID |
| A Claude Pro/Max subscription | Claude subscription | Runs the official Claude Code CLI under your own login. Personal use only |
| Another command-line tool you are signed in to | Another command-line tool | You give the command; it reads the prompt on stdin and prints the answer |

You type the model ID yourself, because IDs change often: copy it from the provider's models page (setup
prints the link). An app subscription such as ChatGPT Plus is not an API key.

Check what is configured:

```bash
uv run swarmos providers
```

Prefer a file? Copy `providers.example.toml` to `providers.toml`; it documents every field.

### About subscriptions

A personal subscription is meant for your own use. SwarmAgentOS runs the vendor's own tool under your login, never
reads or stores that login, sends one call at a time, and caps calls per run and per day (editable in
`providers.toml`). Do not put a subscription behind a service that other people use; use an API key for
that. You are responsible for the provider's terms. Details:
[ADR-005](docs/adr/ADR-005-claude-subscription-adapter.md).

## How to use

**1. Submit an idea** at the top of the page. Three switches:

| Switch | What it does | When to use it |
|---|---|---|
| Ask me first, then dispatch | Chief restates your idea and asks up to 3 questions. Work starts after you answer or skip | On by default. Most useful while the idea is vague |
| Cross-review | Each agent's result is challenged by a different model | On by default. Turn off to save time and money |
| Blind test | One single model also answers alone; you vote between two unsigned answers | While you are deciding whether multi-model helps you |

**2. Answer Chief's questions** in one line each. Blank answers use the assumptions listed under them.

**3. Read the result.** Summary, agreements, disagreements, next steps. The disagreements are where your
attention pays off most. Each agent's own output, assumptions, risks and limits can be opened.

**4. Vote in the blind test.** Progress is hidden. When both sides finish you see A and B and pick the more
useful one; only then does the page say which was which. The yellow strip keeps the score: ten votes,
and the multi-model route passes with six wins.

**Small requests skip the team.** With "Ask me first" on, Chief first decides what kind of request it is. A
simple question gets one direct answer. A request to *do* something (open, send, install) gets a plain
"SwarmAgentOS cannot perform actions" plus the steps you can run yourself. Only real project ideas go to the team.

**Admin page.** http://127.0.0.1:8000/admin shows usage, cost and failures per model, the last 14 days of
spend, and current settings. It is read-only, apart from retrying a model that was paused after repeated
failures. Keys are shown only as present or missing.

**Languages.** The `EN / FR / 中文` switch sets the page language and the output language of the next run,
whatever language the idea is written in. Finished runs keep their language.

**Command line:**

```bash
uv run swarmos run "your idea" --clarify --cross-review --lang en --budget 0.5 --out artifacts/idea.md
```

## Cost and limits

- You pay your providers directly. A run with cross-review is typically 8 to 12 model calls; with small
  models that is a few cents. Each task shows its tokens and cost.
- Every run has a budget ceiling (`--budget`, default $2 from the command line, $0.50 on the page) checked
  before each call, plus a server-side maximum (`SWARMOS_MAX_BUDGET_USD`, default $2).
- Subscription providers are limited to one call at a time, 6 per run and 40 per day by default.
- SwarmAgentOS is local and single-user: no login, answers only on `localhost`. See [SECURITY.md](SECURITY.md).
- More in [Known limitations](docs/ARCHITECTURE.md#known-limitations).

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `SWARMOS_DATABASE_URL` | SQLite file `swarmos.db` in the current folder | For PostgreSQL: `uv sync --extra postgres` and a `postgresql+asyncpg://…` URL |
| `SWARMOS_PROVIDERS_FILE` | `./providers.toml` if present, else demo models | Path to your provider file |
| `SWARMOS_MAX_BUDGET_USD` | `2.0` | Highest budget any run may request |
| `SWARMOS_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | Host names the server answers to |
| `SWARMOS_AUTH_ENABLED` | `true` | Require signup/login (session cookie). `false` restores anonymous single-user mode |
| `SWARMOS_SESSION_TTL_DAYS` | `30` | How long a login session stays valid |

Run options (`swarmos run` flags and `POST /runs` fields):

| Flag / field | Default | Meaning |
|---|---|---|
| `--clarify` / `clarify` | off (on in the page) | Chief asks before dispatching |
| `--cross-review` / `cross_review` | off (on in the page) | One bounded review round |
| `compare`, `baseline_provider` | off | Blind test against one single model |
| `--lang` / `language` | follows the idea | `en`, `fr` or `zh` |
| `--mode` / `mode` | `dynamic` | `dynamic`: Chief routes each task. `fan_out`: every task to every model. `manual`: only `--providers` |
| `--budget` / `budget_usd` | `2.0` | Per-run ceiling in USD |
| `--max-tasks`, `--concurrency` | `6`, `4` | Size and parallelism of the task graph |

## API

| Endpoint | Purpose |
|---|---|
| `GET /` | Command Center page |
| `POST /runs` | Start a run. `?wait=true` blocks until done. `Idempotency-Key` header dedupes repeats |
| `GET /runs`, `GET /runs/{id}` | Recent runs; one run with tasks, usage and result |
| `POST /runs/{id}/answers` | Answer Chief's questions (`{"answers": [...]}`) or `{"skip": true}` |
| `POST /runs/{id}/cancel` | Cancel; finished work is kept |
| `GET /runs/{id}/events`, `/audit` | Event log; every model attempt, policy decision and usage record |
| `GET /comparisons`, `GET /comparisons/{id}` | Blind-test tally; two unsigned answers |
| `POST /comparisons/{id}/vote` | `{"choice": "A"\|"B"\|"tie"}`; reveals which was which |
| `GET /admin`, `GET /admin/summary` | Admin page and its numbers (no secrets) |
| `POST /admin/providers/{id}/reset` | Retry a model paused by the circuit breaker |
| `GET /analytics`, `GET /analytics/summary?days=7\|28\|90\|365` | GA4-style dashboard and its numbers: runs, spend, calls, latency, provider breakdown, run funnel, council score |
| `GET /runs/{id}/receipt`, `GET /runs/{id}/receipt.md` | Decision Receipt for one run — who spoke, what it cost, policy decisions, failures (JSON or Markdown) |
| `GET /providers`, `GET /health` | Configured models (no secrets); liveness (`/health` is public) |
| `GET /login`, `POST /auth/signup` | Sign-up page; create an account (sets the session cookie) |
| `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` | Sign in, sign out, current account |

When auth is on (default), every other endpoint requires a session and returns 401 otherwise, and each
account only sees its own runs, receipts and analytics.

The **Analytics** page (`/analytics`) renders every number from your own ledger tables within a date
window — nothing is hardcoded, so a fresh install shows an empty state and every user sees only their own
runs. The **Decision Receipt** is the proof behind a deliverable: it is built from `agent_runs`,
`usage_records`, `policy_decisions` and `events`, never from a prompt or a key, and is safe to archive or
share.

## Development

```bash
uv run pytest -q
```

Tests use mock models and a temporary SQLite file; none calls a real provider. How the code is organised:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Decisions: [docs/adr/](docs/adr/). Contributing:
[CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

SwarmAgentOS is free software under the [GNU AGPL v3 or later](LICENSE): you may use, modify and share it, and if
you run a modified version as a network service you must publish your changes under the same licence.

If you want to use SwarmAgentOS in a product or service without those obligations, a commercial licence is
available: open an issue titled "Commercial licence".
