"""`swarmos setup`: connect your own models.

Adds one provider per run to ``providers.toml`` and, for API providers, stores the key in ``.env``
(mode 600, gitignored). The key is read with hidden input, never printed, never passed on a command line,
and never written to ``providers.toml``.

Model IDs are typed by the user, because they change often: check the provider's own models page.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ALL_CAPABILITIES = ["reasoning", "planning", "synthesis", "review", "research", "code", "json", "long_context"]


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    kind: str  # api | compatible | claude | command
    prefix: str = ""  # LiteLLM model prefix
    key_env: str = ""
    api_base: str | None = None
    models_url: str | None = None  # GET with a Bearer key lists model IDs (also proves the key works)
    key_checked_by_listing: bool = True
    docs: str = ""
    known_models: tuple[str, ...] = field(default_factory=tuple)


PRESETS: list[Preset] = [
    Preset("deepseek", "DeepSeek (API key)", "api", "deepseek/", "DEEPSEEK_API_KEY", "https://api.deepseek.com",
           "https://api.deepseek.com/models", docs="https://api-docs.deepseek.com/quick_start/pricing"),
    Preset("openai", "OpenAI (API key)", "api", "openai/", "OPENAI_API_KEY", None,
           "https://api.openai.com/v1/models", docs="https://platform.openai.com/docs/models"),
    Preset("anthropic", "Anthropic Claude (API key)", "api", "anthropic/", "ANTHROPIC_API_KEY",
           docs="https://docs.anthropic.com/en/docs/about-claude/models"),
    Preset("gemini", "Google Gemini (API key)", "api", "gemini/", "GEMINI_API_KEY",
           docs="https://ai.google.dev/gemini-api/docs/models"),
    Preset("openrouter", "OpenRouter (one key, many models)", "api", "openrouter/", "OPENROUTER_API_KEY", None,
           "https://openrouter.ai/api/v1/models", key_checked_by_listing=False, docs="https://openrouter.ai/models"),
    Preset("xai", "xAI Grok (API key)", "api", "xai/", "XAI_API_KEY", docs="https://docs.x.ai/docs/models"),
    Preset("compatible", "Any OpenAI-compatible endpoint (Qwen, Kimi, GLM, Ollama, your own gateway...)",
           "compatible", "openai/"),
    Preset("claude", "Claude Pro/Max subscription, through the Claude Code CLI (personal use only)", "claude"),
    Preset("command", "Another command-line tool you are signed in to (your own subscription or local model)",
           "command"),
]

SUBSCRIPTION_NOTICE = """
Read this before connecting a subscription:
  * It uses your own login on this machine. SwarmAgentOS never reads or stores the login.
  * A personal subscription is for your own use. Do not serve other people through it;
    for that, use an API key instead.
  * SwarmAgentOS keeps the volume low: one call at a time, with per-run and per-day caps you can edit.
  * You are responsible for the provider's terms. Breaking them can get an account suspended.
"""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "provider"


def existing_ids(providers_path: Path) -> set[str]:
    if not providers_path.exists():
        return set()
    data = tomllib.loads(providers_path.read_text(encoding="utf-8"))
    return {str(p.get("id")) for p in data.get("providers", [])}


def toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)  # JSON string syntax is valid TOML basic-string syntax


def provider_block(entry: dict[str, object]) -> str:
    lines = ["", "[[providers]]"]
    lines += [f"{k} = {toml_value(v)}" for k, v in entry.items() if v is not None]
    return "\n".join(lines) + "\n"


def append_provider(providers_path: Path, entry: dict[str, object]) -> None:
    """Append one block. Existing content, including comments, is left untouched."""
    if str(entry["id"]) in existing_ids(providers_path):
        raise ValueError(f"a provider with id {entry['id']!r} already exists in {providers_path}")
    if "api_key" in entry:
        raise ValueError("keys never go in providers.toml")
    header = "" if providers_path.exists() else (
        "# SwarmAgentOS providers. Created by `swarmos setup`. Keys live in .env, never here.\n"
        "# Fields: see providers.example.toml.\n")
    with providers_path.open("a", encoding="utf-8") as f:
        f.write(header + provider_block(entry))
    tomllib.loads(providers_path.read_text(encoding="utf-8"))  # fail loudly if the file is now invalid


def save_secret(env_path: Path, name: str, value: str) -> None:
    """Upsert NAME=value in .env with owner-only permissions."""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        raise ValueError("environment variable names use capital letters, digits and underscores")
    if any(c in value for c in "\r\n"):
        raise ValueError("the key contains a line break")
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    kept = [ln for ln in lines if not re.match(rf"^(export\s+)?{re.escape(name)}=", ln)]
    env_path.touch(mode=0o600, exist_ok=True)
    env_path.chmod(0o600)
    env_path.write_text("\n".join([*kept, f"{name}={value}"]) + "\n", encoding="utf-8")


def list_models(url: str, key: str, timeout: float = 15.0) -> tuple[bool | None, list[str]]:
    """(key accepted?, model ids). None means the check could not be made (network problem)."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": "swarmos-setup"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URLs or the user's own
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        return (False if e.code in (401, 403) else None), []
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None, []
    return True, [str(m.get("id")) for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]


def run_setup(ask: Callable[[str], str], ask_secret: Callable[[str], str], say: Callable[[str], None],
              providers_path: Path = Path("providers.toml"), env_path: Path = Path(".env"),
              check: Callable[[str, str], tuple[bool | None, list[str]]] = list_models) -> int:
    say("Connect a model to SwarmAgentOS. Nothing is sent anywhere except the provider you choose.\n")
    for i, p in enumerate(PRESETS, 1):
        say(f"  {i}. {p.label}")
    choice = ask(f"\nChoose 1-{len(PRESETS)}: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(PRESETS):
        say("Not a valid choice. Nothing changed.")
        return 1
    preset = PRESETS[int(choice) - 1]
    entry: dict[str, object]

    if preset.kind in ("claude", "command"):
        say(SUBSCRIPTION_NOTICE)
        if ask('Type "I understand" to continue: ').strip().lower() != "i understand":
            say("Cancelled. Nothing changed.")
            return 1
        if preset.kind == "claude":
            model = ask("Claude model ID or alias (for example sonnet; see `claude --help`): ").strip()
            entry = {"id": "claude-subscription", "display_name": f"Claude {model} (your subscription)",
                     "adapter": "claude_cli", "model": model}
        else:
            say("\nThe tool must read the prompt on standard input and print its answer on standard output.")
            say("Write {model} where the model name goes, if the tool takes one.")
            cmd = shlex.split(ask("Command line: "))
            if not cmd:
                say("No command given. Nothing changed.")
                return 1
            model = ask("Model name (or a label for this tool): ").strip()
            entry = {"id": slug(model or cmd[0]), "display_name": f"{model} (command)",
                     "adapter": "command", "model": model, "command": cmd}
        if not model:
            say("No model given. Nothing changed.")
            return 1
        entry |= {"capabilities": ALL_CAPABILITIES, "cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0,
                  "max_concurrency": 1, "max_calls_per_run": 6, "max_calls_per_day": 40, "enabled": True}
    else:
        api_base, key_env, models_url = preset.api_base, preset.key_env, preset.models_url
        name = preset.key
        if preset.kind == "compatible":
            name = slug(ask("A short name for this provider (for example qwen, kimi, ollama): "))
            api_base = ask("Base URL (for example https://host/v1): ").strip().rstrip("/")
            if not api_base.startswith(("https://", "http://localhost", "http://127.0.0.1")):
                say("Use an https:// URL (plain http is only accepted for localhost). Nothing changed.")
                return 1
            key_env = f"{name.upper().replace('-', '_')}_API_KEY"
            models_url = f"{api_base}/models"
        if preset.docs:
            say(f"\nCurrent model IDs: {preset.docs}")
        key = ask_secret(f"Paste your {preset.label.split(' (')[0] if preset.kind == 'api' else name} API key "
                         "(input hidden; leave empty if the endpoint needs none): ").strip()
        if not key and preset.kind == "api":
            say("No key entered. Nothing changed.")
            return 1
        ids: list[str] = []
        if models_url and key:
            ok, ids = check(models_url, key)
            if ok is False:
                say("The provider rejected this key. Nothing changed.")
                return 1
            if ok is None:
                say("Could not reach the provider to check the key. It will be tested on the first run.")
            elif preset.key_checked_by_listing:
                say("Key accepted.")
            if ids:
                say("Models this key can use include: " + ", ".join(sorted(ids)[:12])
                    + (" ..." if len(ids) > 12 else ""))
        model = ask("Model ID: ").strip()
        if not model:
            say("No model given. Nothing changed.")
            return 1
        if ids and model not in ids:
            say(f"Note: {model!r} is not in the list the provider returned. Saved anyway; check the spelling.")
        entry = {"id": slug(f"{name}-{model.split('/')[-1]}"), "display_name": f"{model} ({name})",
                 "adapter": "litellm", "model": preset.prefix + model, "api_base": api_base,
                 "api_key_env": key_env if key else None, "capabilities": ALL_CAPABILITIES, "enabled": True}
        if key:
            save_secret(env_path, key_env, key)
            say(f"Key saved to {env_path} as {key_env} (owner-only file, ignored by git).")

    try:
        append_provider(providers_path, entry)
    except ValueError as e:
        say(f"{e}. Edit that file to change it.")
        return 1
    say(f"Added {entry['id']} to {providers_path}.")
    say("Run `swarmos setup` again to add another model. SwarmAgentOS works best with two or more different models.")
    say("Then start it with: swarmos serve")
    return 0


def describe(providers_path: Path = Path("providers.toml")) -> list[str]:
    """Lines for `swarmos providers`: what is configured and whether its key is present. No secret values."""
    from swarmos.gateway.registry import load_provider_configs

    if not providers_path.exists():
        return ["No providers.toml here: SwarmAgentOS is in demo mode with built-in mock models.",
                "Run `swarmos setup` to connect a real model."]
    out = []
    for c in load_provider_configs(providers_path):
        kind = {"litellm": "API", "claude_cli": "subscription", "command": "command", "mock": "mock"}[c.adapter]
        key = "" if not c.api_key_env else (" key set" if os.environ.get(c.api_key_env) else f" MISSING {c.api_key_env}")
        out.append(f"{'on ' if c.enabled else 'off'} {c.id:32} {kind:12} {c.model}{key}")
    return out
