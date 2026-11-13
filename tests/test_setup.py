"""`swarmos setup`, the command adapter, and zero-config defaults."""

from __future__ import annotations

import stat

import pytest

from swarmos.config import Settings
from swarmos.contracts import RunRequest, RunStatus
from swarmos.gateway.base import CompletionRequest, ProviderConfig, ProviderError
from swarmos.gateway.command import CommandAdapter
from swarmos.gateway.registry import ProviderRegistry, load_provider_configs
from swarmos.setup import PRESETS, append_provider, run_setup, save_secret

SECRET = "sk-setup-DO-NOT-LEAK-9"


def _wizard(tmp_path, answers, secret=SECRET, check=lambda url, key: (True, ["model-a", "model-b"])):
    said: list[str] = []
    it = iter(answers)
    code = run_setup(lambda prompt: next(it), lambda prompt: secret, said.append,
                     providers_path=tmp_path / "providers.toml", env_path=tmp_path / ".env", check=check)
    return code, "\n".join(said)


def _choice(key: str) -> str:
    return str([p.key for p in PRESETS].index(key) + 1)


def test_api_key_setup_writes_reference_not_secret(tmp_path):
    code, said = _wizard(tmp_path, [_choice("deepseek"), "model-a"])
    assert code == 0 and SECRET not in said
    toml = (tmp_path / "providers.toml").read_text()
    assert SECRET not in toml and 'api_key_env = "DEEPSEEK_API_KEY"' in toml and 'model = "deepseek/model-a"' in toml
    env = tmp_path / ".env"
    assert f"DEEPSEEK_API_KEY={SECRET}" in env.read_text() and stat.S_IMODE(env.stat().st_mode) == 0o600
    cfg = load_provider_configs(tmp_path / "providers.toml")[0]
    assert cfg.adapter == "litellm" and cfg.enabled and "planning" in cfg.capabilities


def test_rejected_key_changes_nothing(tmp_path):
    code, said = _wizard(tmp_path, [_choice("openai"), "gpt-x"], check=lambda u, k: (False, []))
    assert code == 1 and "rejected" in said
    assert not (tmp_path / "providers.toml").exists() and not (tmp_path / ".env").exists()


def test_compatible_endpoint_and_second_provider_append(tmp_path):
    (tmp_path / "providers.toml").write_text('# my notes stay\n[[providers]]\nid = "old"\nadapter = "mock"\nmodel = "m"\n')
    code, _ = _wizard(tmp_path, [_choice("compatible"), "Qwen", "https://example.test/v1", "qwen-model"],
                      check=lambda u, k: (None, []))
    assert code == 0
    text = (tmp_path / "providers.toml").read_text()
    assert text.startswith("# my notes stay") and 'api_base = "https://example.test/v1"' in text
    assert 'api_key_env = "QWEN_API_KEY"' in text and 'model = "openai/qwen-model"' in text
    assert {c.id for c in load_provider_configs(tmp_path / "providers.toml")} == {"old", "qwen-qwen-model"}
    code, said = _wizard(tmp_path, [_choice("compatible"), "Qwen", "http://evil.example/v1", "m"])
    assert code == 1 and "https" in said  # plain http only for localhost


def test_subscription_needs_explicit_acknowledgement(tmp_path):
    code, said = _wizard(tmp_path, [_choice("claude"), "yes", "sonnet"])
    assert code == 1 and "personal subscription" in said and not (tmp_path / "providers.toml").exists()
    code, _ = _wizard(tmp_path, [_choice("claude"), "I understand", "sonnet"])
    cfg = load_provider_configs(tmp_path / "providers.toml")[0]
    assert code == 0 and cfg.adapter == "claude_cli" and cfg.max_concurrency == 1
    assert cfg.max_calls_per_run == 6 and cfg.max_calls_per_day == 40 and cfg.api_key_env is None


def test_duplicate_id_and_inline_key_are_refused(tmp_path):
    p = tmp_path / "providers.toml"
    append_provider(p, {"id": "a", "adapter": "mock", "model": "m"})
    with pytest.raises(ValueError, match="already exists"):
        append_provider(p, {"id": "a", "adapter": "mock", "model": "m"})
    with pytest.raises(ValueError, match="never go"):
        append_provider(p, {"id": "b", "adapter": "litellm", "model": "m", "api_key": "sk-x"})


def test_save_secret_replaces_old_value(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nMY_API_KEY=old\n")
    save_secret(env, "MY_API_KEY", "new")
    assert env.read_text() == "OTHER=1\nMY_API_KEY=new\n"
    with pytest.raises(ValueError):
        save_secret(env, "bad name", "x")


# ---------------------------------------------------------------- command adapter
def _tool(tmp_path, body: str):
    path = tmp_path / "mytool"
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


async def test_command_adapter_runs_user_tool_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    tool = _tool(tmp_path, 'import os, sys, json\np = sys.stdin.read()\n'
                           'print(json.dumps({"summary": sys.argv[2] + ":" + str(len(p)), "leak": "OPENAI_API_KEY" in os.environ}))\n')
    cfg = ProviderConfig(id="mine", adapter="command", model="local-7", command=[tool, "--model", "{model}"])
    resp = await CommandAdapter(cfg).complete(CompletionRequest(model="local-7", messages=[
        {"role": "system", "content": "SYS"}, {"role": "user", "content": "hello"}]))
    assert '"summary": "local-7:10"' in resp.text and '"leak": false' in resp.text and resp.cost_usd == 0.0


async def test_command_adapter_failure_and_missing_binary(tmp_path):
    bad = ProviderConfig(id="b", adapter="command", model="m", command=[_tool(tmp_path, "import sys\nsys.exit(2)\n")])
    with pytest.raises(ProviderError) as e:
        await CommandAdapter(bad).complete(CompletionRequest(model="m", messages=[]))
    assert e.value.kind == "cli_error"
    gone = ProviderConfig(id="g", adapter="command", model="m", command=["/nonexistent/tool"])
    with pytest.raises(ProviderError) as e:
        await CommandAdapter(gone).complete(CompletionRequest(model="m", messages=[]))
    assert e.value.kind == "missing_binary"
    with pytest.raises(ValueError):
        CommandAdapter(ProviderConfig(id="x", adapter="command", model="m"))


# ---------------------------------------------------------------- zero-config defaults
def test_defaults_need_no_database_server_or_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("SWARMOS_DATABASE_URL", "SWARMOS_PROVIDERS_FILE"):
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env()
    assert s.database_url.startswith("sqlite") and s.providers_file is None  # demo models
    (tmp_path / "providers.toml").write_text('[[providers]]\nid = "x"\nadapter = "mock"\nmodel = "m"\n')
    assert Settings.from_env().providers_file == "providers.toml"
    monkeypatch.setenv("SWARMOS_PROVIDERS_FILE", "")
    assert Settings.from_env().providers_file is None  # explicit empty value forces demo models


async def test_parallel_run_on_a_fresh_sqlite_file(tmp_path):
    from swarmos.orchestrator import Orchestrator
    from swarmos.store import Store
    from tests.conftest import mock_cfg
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    await store.init()
    try:
        orch = Orchestrator(Settings(retry_backoff_s=0), ProviderRegistry.from_configs(
            [mock_cfg("a"), mock_cfg("b"), mock_cfg("c")]), store)
        d = await orch.run(RunRequest(idea="Fresh install smoke test", cross_review=True))
        assert d.status is RunStatus.completed
    finally:
        await store.close()
