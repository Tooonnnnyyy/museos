"""Settings from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def load_dotenv(path: str = ".env") -> list[str]:
    """Load KEY=VALUE lines from ``path`` into os.environ without overriding variables already set.

    Returns the names loaded (never the values). Blank values are skipped so an empty
    ``DEEPSEEK_API_KEY=`` line does not count as a configured key.
    """
    loaded: list[str] = []
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return loaded
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def _providers_file() -> str | None:
    """SWARMOS_PROVIDERS_FILE if set (empty = force demo models), else ./providers.toml when it exists."""
    if "SWARMOS_PROVIDERS_FILE" in os.environ:
        return os.environ["SWARMOS_PROVIDERS_FILE"] or None
    return "providers.toml" if os.path.exists("providers.toml") else None


def _bool(v: str | None, default: bool) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    database_url: str = "sqlite+aiosqlite:///swarmos.db"  # a file in the working directory; no install needed
    providers_file: str | None = None  # TOML; unset = ./providers.toml if present, else built-in demo models
    retry_backoff_s: float = 0.25
    external_writes_enabled: bool = False
    destination_allowlist: set[str] = field(default_factory=set)
    # The API has no login. It only answers requests addressed to these hosts (blocks DNS rebinding),
    # and no caller can ask for more than max_budget_usd per run.
    allowed_hosts: list[str] = field(default_factory=lambda: ["localhost", "127.0.0.1", "[::1]"])
    max_budget_usd: float = 2.0
    # Authentication: when True, the API requires a session cookie (signup/login) and scopes every
    # run to the signed-in user's workspace. The CLI (`swarmos run`) bypasses the API and is unaffected.
    auth_enabled: bool = True
    session_ttl_days: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ.get("SWARMOS_DATABASE_URL", cls.database_url),
            providers_file=_providers_file(),
            retry_backoff_s=float(os.environ.get("SWARMOS_RETRY_BACKOFF_S", "0.25")),
            external_writes_enabled=_bool(os.environ.get("SWARMOS_EXTERNAL_WRITES_ENABLED"), False),
            allowed_hosts=[h.strip() for h in os.environ.get("SWARMOS_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
                           if h.strip()],
            max_budget_usd=float(os.environ.get("SWARMOS_MAX_BUDGET_USD", "2.0")),
            destination_allowlist={d.strip() for d in os.environ.get("SWARMOS_DESTINATION_ALLOWLIST", "").split(",")
                                   if d.strip()},
            auth_enabled=_bool(os.environ.get("SWARMOS_AUTH_ENABLED"), True),
            session_ttl_days=int(os.environ.get("SWARMOS_SESSION_TTL_DAYS", "30")),
        )
