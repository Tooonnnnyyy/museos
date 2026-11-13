# ADR-005: Claude through the Claude Code CLI on a personal subscription

Status: accepted (2026-11-08)

## Context

Many users have a Claude Pro or Max subscription and no Anthropic API key. A subscription does not include API access.
Anthropic's published rules (code.claude.com/docs/en/legal-and-compliance, read 2026-11-08) say:

- OAuth login is for subscribers and "ordinary use of Claude Code and other native Anthropic applications".
- Plan limits "assume ordinary, individual usage of Claude Code and the Agent SDK".
- Developers building products must use API keys, must not route requests through plan credentials on behalf
  of their users, and must not collect, store or intermediate Claude.ai credentials or session tokens.
- An end user may sign in to the unmodified Claude Code binary with their own subscription.

## Decision

Add a `claude_cli` adapter that runs the unmodified `claude` binary in headless mode (`claude -p`) under the
user's own login, for that user's own runs on their own machine.

Guardrails, all enforced in code:

1. No credential handling. SwarmAgentOS never reads the keychain or any token. `--bare` is not used because it
   bypasses the subscription login.
2. Text only. `--tools ""`, `--strict-mcp-config`, `--disable-slash-commands`, `--setting-sources ""`,
   `--no-session-persistence`, run from an empty temp directory. The SwarmAgentOS policy engine stays the only gate.
3. Secrets stay out. Provider keys and `ANTHROPIC_*` variables are removed from the child's environment, so a
   call cannot silently bill an API key either.
4. Ordinary volume. `max_concurrency = 1`, `max_calls_per_run`, `max_calls_per_day`; a cap hit is recorded as
   a `provider_quota` policy denial and the task falls back to another provider.
5. No hammering. A usage-limit or overload reply is non-retryable.

## Consequences

- This provider is single-user. Before SwarmAgentOS serves anyone else (hosted API, Telegram bot for other people,
  team use), Claude calls must move to an Anthropic API key through the LiteLLM adapter. The switch is a
  `providers.toml` change.
- Calls share the user's interactive Claude limits and are slower than API calls (one process per call).
- Cost is booked as $0. The CLI's `total_cost_usd` is a notional figure, not a bill.
- Account enforcement is Anthropic's decision. These guardrails follow the published rules; they are not a
  guarantee.

The generic `command` adapter (`gateway/command.py`) applies the same pattern to any other command-line tool
a user is signed in to. SwarmAgentOS does not ship settings for other vendors' CLIs: each user configures the command
and is responsible for that vendor's terms. `swarmos setup` shows a notice and requires an explicit
acknowledgement before adding any subscription-backed provider.
