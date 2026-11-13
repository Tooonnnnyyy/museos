"""Claude via the official Claude Code CLI, using the user's own Claude subscription login.

Rules this adapter follows (Anthropic, code.claude.com/docs/en/legal-and-compliance):
  * It runs the unmodified ``claude`` binary as published. It never reads, copies, stores
    or forwards OAuth tokens or session credentials; sign-in stays inside Claude Code.
  * It is for the subscriber's own, local, ordinary use. Do not use it to serve other
    people's requests: for that, use an Anthropic API key with the LiteLLM adapter.
  * Volume stays low: the registry entry should set max_concurrency = 1 and the
    max_calls_per_run / max_calls_per_day caps, which the engine enforces.

Each call is a single text turn with every Claude Code tool, MCP server, slash command and
settings file disabled, run from an empty temporary directory, so SwarmAgentOS's policy engine
stays the only gate on actions. Other providers' secrets are removed from the child's
environment, and ANTHROPIC_API_KEY is removed so the call cannot silently bill an API key.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile

from swarmos.gateway.base import CompletionRequest, CompletionResponse, ProviderConfig, ProviderError

_SECRET_ENV = re.compile(r"(_API_KEY|_TOKEN|_SECRET|PASSWORD)$|^ANTHROPIC_|^SWARMOS_", re.IGNORECASE)
_LIMIT_HINTS = ("usage limit", "rate limit", "limit reached", "overloaded", "too many requests", "529", "429")
_AUTH_HINTS = ("not logged in", "/login", "authentication", "unauthorized", "invalid api key", "401")


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}


def build_args(binary: str, model: str, system: str) -> list[str]:
    return [binary, "-p", "--output-format", "json", "--model", model,
            "--tools", "", "--strict-mcp-config", "--no-session-persistence",
            "--disable-slash-commands", "--setting-sources", "", "--system-prompt", system]


def _classify(text: str) -> ProviderError:
    low = text.lower()
    if any(h in low for h in _AUTH_HINTS):
        return ProviderError("Claude Code is not signed in; run `claude` once and log in", retryable=False,
                             kind="missing_credential")
    if any(h in low for h in _LIMIT_HINTS):
        # Do not hammer a subscription that is at its limit: fail over instead of retrying.
        return ProviderError("Claude subscription limit or overload reported", retryable=False,
                             kind="subscription_limit")
    return ProviderError("Claude Code call failed", retryable=True, kind="cli_error")


class ClaudeCLIAdapter:
    def __init__(self, config: ProviderConfig):
        self.config = config
        self.provider_id = config.id
        self.binary = config.api_base or "claude"  # api_base may hold a path to the binary

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        binary = shutil.which(self.binary)
        if not binary:
            raise ProviderError("Claude Code CLI not found on PATH", retryable=False, kind="missing_binary")
        system = "\n\n".join(m["content"] for m in request.messages if m.get("role") == "system")
        prompt = "\n\n".join(m["content"] for m in request.messages if m.get("role") != "system")

        with tempfile.TemporaryDirectory(prefix="swarmos-claude-") as workdir:
            proc = await asyncio.create_subprocess_exec(
                *build_args(binary, request.model, system), cwd=workdir, env=child_env(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                try:
                    proc.stdin.write(prompt.encode())
                    await proc.stdin.drain()
                    proc.stdin.close()
                except (BrokenPipeError, ConnectionResetError, RuntimeError):
                    pass  # the CLI exited before reading its input; its exit code and stderr say why
                out, err = await proc.communicate()
            except asyncio.CancelledError:  # engine timeout or run cancel
                proc.kill()
                await proc.wait()
                raise

        try:
            data = json.loads(out.decode() or "{}")
        except json.JSONDecodeError:
            data = {}
        if proc.returncode != 0 or data.get("is_error") or data.get("subtype") not in (None, "success"):
            raise _classify(f"{data.get('result', '')} {data.get('subtype', '')} {err.decode()[-500:]}")

        usage = data.get("usage") or {}
        in_tok = int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0)) \
            + int(usage.get("cache_creation_input_tokens", 0))
        return CompletionResponse(
            text=str(data.get("result", "")),
            provider=self.provider_id,
            model=next(iter(data.get("modelUsage") or {}), request.model),
            input_tokens=in_tok,
            output_tokens=int(usage.get("output_tokens", 0)),
            # Covered by the subscription: no per-call charge. total_cost_usd in the CLI output is
            # a notional API-equivalent figure, not a bill, so it is not booked against the budget.
            cost_usd=0.0,
            latency_ms=int(data.get("duration_ms", 0)),
            finish_reason="stop",
        )
