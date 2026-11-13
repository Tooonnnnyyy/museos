# ADR-007: Read-only web tools for research tasks

**Status:** Accepted · 2026-11-13

## Context

The architecture documented "agents have no tools" as a known limitation: answers were reasoning
without sources. Research tasks needed verified web input, but adding a free-form tool loop would
break the bounded one-shot design and the "policy decides before anything runs" guarantee.

## Decision

- **No tool-calling loop.** A research task's context is enriched *before* its single model call:
  one web search plus up to three fetches of the top hits, appended to the prompt as "Verified web
  sources", with the instruction to cite URLs in `evidence` entries. Bounded by construction.
- **Tools**: `web_search` (DuckDuckGo HTML endpoint, key-less, with Instant Answer fallback) and
  `web_fetch` (httpx, byte-capped, http/https only, SSRF-guarded: hosts resolving to private/loopback/
  link-local/reserved addresses are refused). HTML is reduced to text with the stdlib parser.
- **Policy first**: every call goes through the existing `PolicyEngine` as `tool.<name>` with the
  task's `tools_allowed`; denials and allows are recorded in `policy_decisions`, outcomes in a new
  `tool_runs` table, and `tool.used` events in the run ledger. Tools cost $0 but consume a budget step.
- **Two switches**: server-level `SWARMOS_TOOLS_ENABLED` (default on) and per-run `research: true`
  on the request. The Chief marks `tools_allowed = ["web_search", "web_fetch"]` on tasks that ask
  for research capability. Default runs stay offline.
- **Dogfooding**: `swarmos pr-review <owner/repo> <n>` fetches a PR via the user's `gh` CLI and runs
  the swarm on it (cross-review on, optionally `--research`), saving the review to `artifacts/` and
  optionally posting it as a PR comment with `--post`. No GitHub credentials are stored: it reuses
  the user's existing gh auth.

## Consequences

- Research answers now carry sources; the receipt lists every tool call with its policy decision.
- SSRF is guarded at the host-resolution layer; the fetch parser is best-effort (blocked by design,
  not by sandbox) — treat fetched content as untrusted prompt data, same as any user input.
- If richer research is needed later (multiple search rounds, more sources), extend the enrichment
  step, not the agent loop.
