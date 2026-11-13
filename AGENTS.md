# AGENTS.md — read me first

You are working in **SwarmAgentOS (蜂巢)** — `Tooonnnnyyy/museos`. A local multi-model orchestration
engine: one idea in → Chief plans a task graph → parallel specialist agents on different models →
one bounded cross-review round → one merged answer, plus a blind test that measures whether the
multi-model route is worth it.

## Before any work

1. Read `CHANGELOG.md` — it is the handoff log: what landed, what is in progress, how to verify.
2. Read `CLAUDE.md` — architecture rules that are easy to break.
3. If you are one of several agents in this repo, read `WORKFLOW.md` and follow its rule:
   **one agent = one worktree + one branch; never two agents in the same directory.**

## Hard constraints

- Every model call goes through `engine.call_model`; never call a provider adapter directly.
- Secrets never leave env vars: no keys in TOML, prompts, DB rows, logs or chat.
- No free-form tool loop and no external writes. The only tools are the read-only web ones, gated by
  `research: true` + `SWARMOS_TOOLS_ENABLED` + `PolicyEngine` (ADR-007).
- Auth is on by default; every data endpoint is scoped to `workspace_id = user:<id>` (ADR-006).
- Three languages (en/fr/zh) for every user-facing string; run `pytest` before claiming anything works.

## Commands

```bash
uv sync && uv run pytest -q                     # verify (91 passed, 1 skipped)
uv run swarmos serve                            # http://127.0.0.1:8000
uv run swarmos run "idea" --research --cross-review
uv run swarmos pr-review <owner/repo> <n> --post
```

## Handoff

When you finish a work chunk: commit on a branch, update `CHANGELOG.md` (what you did, what is left,
how to verify), and open a PR. Never push straight to `main` without review.
