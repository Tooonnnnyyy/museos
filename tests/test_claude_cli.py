"""Claude Code CLI adapter (against a fake binary) and per-provider call caps."""

from __future__ import annotations

import json
import stat

import pytest

from museos.contracts import RunRequest, RunStatus
from museos.gateway.base import CompletionRequest, ProviderConfig, ProviderError
from museos.gateway.claude_cli import ClaudeCLIAdapter
from tests.conftest import mock_cfg

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
prompt = sys.stdin.read()
json.dump({"argv": sys.argv[1:], "env": sorted(os.environ), "cwd": os.getcwd(), "prompt": prompt},
          open(os.environ["FAKE_LOG"], "w"))
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "limit":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "Claude usage limit reached. Your limit will reset at 5pm"}))
    sys.exit(1)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": '{"summary": "ok"}',
                  "total_cost_usd": 0.01, "duration_ms": 1200, "modelUsage": {"claude-sonnet-x": {}},
                  "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 7}}))
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "log.json"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-pass")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-should-not-pass")
    cfg = ProviderConfig(id="claude", adapter="claude_cli", model="sonnet", api_base=str(path))
    return ClaudeCLIAdapter(cfg), log


def _req():
    return CompletionRequest(model="sonnet", messages=[{"role": "system", "content": "SYS"},
                                                       {"role": "user", "content": "USER PROMPT"}])


async def test_cli_call_is_toolless_and_strips_secrets(fake_claude, tmp_path):
    adapter, log = fake_claude
    resp = await adapter.complete(_req())
    seen = json.loads(log.read_text())
    argv = seen["argv"]
    assert argv[:3] == ["-p", "--output-format", "json"]
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == ""
    assert {"--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"} <= set(argv)
    assert "--bare" not in argv  # --bare would bypass the subscription login
    assert argv[argv.index("--system-prompt") + 1] == "SYS"
    assert seen["prompt"] == "USER PROMPT" and "USER PROMPT" not in argv  # prompt via stdin
    assert "ANTHROPIC_API_KEY" not in seen["env"] and "DEEPSEEK_API_KEY" not in seen["env"]
    assert "museos-claude-" in seen["cwd"]  # empty temp dir, no project files or CLAUDE.md
    assert resp.text == '{"summary": "ok"}' and resp.cost_usd == 0.0
    assert resp.model == "claude-sonnet-x" and resp.input_tokens == 15 and resp.output_tokens == 7


async def test_cli_subscription_limit_is_not_retried(fake_claude, monkeypatch):
    adapter, _ = fake_claude
    monkeypatch.setenv("FAKE_MODE", "limit")
    with pytest.raises(ProviderError) as e:
        await adapter.complete(_req())
    assert e.value.kind == "subscription_limit" and e.value.retryable is False


async def test_cli_missing_binary():
    cfg = ProviderConfig(id="c", adapter="claude_cli", model="m", api_base="/nonexistent/claude")
    with pytest.raises(ProviderError) as e:
        await ClaudeCLIAdapter(cfg).complete(_req())
    assert e.value.kind == "missing_binary"


# ---------------------------------------------------------------- call caps
def _capped(**caps):
    a = mock_cfg("capped", {"reasoning", "planning", "json", "synthesis", "review", "research", "code"})
    for k, v in caps.items():
        setattr(a, k, v)
    a.cost_per_1k_input = a.cost_per_1k_output = 0.0  # cheapest, so the router prefers it
    return [a, mock_cfg("other")]


async def test_max_calls_per_run_falls_back(make_orch, store):
    orch = make_orch(_capped(max_calls_per_run=2))
    d = await orch.run(RunRequest(idea="Cap test", max_tasks=4))
    assert d.status is RunStatus.completed
    runs = await store.list_agent_runs(d.run_id)
    assert sum(a.provider == "capped" for a in runs) == 2
    denials = [p for p in await store.list_policy_decisions(d.run_id) if p.rule == "provider_quota"]
    assert denials and all("max_calls_per_run=2" in p.reason for p in denials)


async def test_max_calls_per_day_spans_runs(make_orch, store):
    orch = make_orch(_capped(max_calls_per_day=3))
    first = await orch.run(RunRequest(idea="Day cap 1", max_tasks=2))
    second = await orch.run(RunRequest(idea="Day cap 2", max_tasks=2))
    used = [a for r in (first, second) for a in await store.list_agent_runs(r.run_id) if a.provider == "capped"]
    assert len(used) == 3
    assert second.status is RunStatus.completed


async def test_cli_that_exits_before_reading_input_is_a_failed_attempt(tmp_path):
    path = tmp_path / "claude"
    path.write_text("#!/bin/sh\nexec 0<&-\necho 'boom' >&2\nexit 3\n")  # closes stdin and dies at once
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    cfg = ProviderConfig(id="c", adapter="claude_cli", model="m", api_base=str(path))
    with pytest.raises(ProviderError) as e:
        await ClaudeCLIAdapter(cfg).complete(CompletionRequest(model="m", messages=[
            {"role": "user", "content": "x" * 200_000}]))  # larger than the pipe buffer
    assert e.value.kind == "cli_error" and e.value.retryable is True


async def test_adapter_crash_is_recorded_and_falls_back(make_orch, store, monkeypatch):
    orch = make_orch(_capped())

    async def boom(request):
        raise RuntimeError("the handler is closed")

    monkeypatch.setattr(orch.registry.adapters["capped"], "complete", boom)
    d = await orch.run(RunRequest(idea="Adapter crash", max_tasks=2))
    assert d.status is RunStatus.completed and d.providers_used == ["other"]
    crashed = [a for a in await store.list_agent_runs(d.run_id) if a.provider == "capped"]
    assert crashed and all(a.error == "adapter_error: RuntimeError" for a in crashed)
    assert {t.status for t in await store.list_tasks(d.run_id)} == {"completed"}


async def test_provider_concurrency_limit_is_shared_across_runs(make_orch):
    from museos.gateway.mock import MockProvider
    cfg = mock_cfg("solo", latency_s=0.15)
    cfg.max_concurrency = 1
    orch = make_orch([cfg])
    import asyncio
    await asyncio.gather(orch.run(RunRequest(idea="Run one", max_tasks=2)),
                         orch.run(RunRequest(idea="Run two", max_tasks=2)))
    assert MockProvider.max_inflight == 1
