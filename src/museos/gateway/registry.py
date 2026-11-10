"""Provider registry, config loading, and a simple circuit breaker."""

from __future__ import annotations

import asyncio

import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from museos.gateway.base import ModelProviderAdapter, ProviderConfig


def default_mock_configs() -> list[ProviderConfig]:
    """Three mock providers with different strengths, used when no config file exists."""
    return [
        ProviderConfig(id="mock-alpha", adapter="mock", model="mock-alpha-1", display_name="Mock Alpha",
                       capabilities={"reasoning", "planning", "json", "synthesis"},
                       cost_per_1k_input=0.001, cost_per_1k_output=0.002),
        ProviderConfig(id="mock-beta", adapter="mock", model="mock-beta-1", display_name="Mock Beta",
                       capabilities={"reasoning", "json", "code", "review"},
                       cost_per_1k_input=0.001, cost_per_1k_output=0.002),
        ProviderConfig(id="mock-gamma", adapter="mock", model="mock-gamma-1", display_name="Mock Gamma",
                       capabilities={"reasoning", "json", "research", "long_context"},
                       cost_per_1k_input=0.0005, cost_per_1k_output=0.001),
    ]


def load_provider_configs(path: str | Path | None) -> list[ProviderConfig]:
    if not path:
        return default_mock_configs()
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"providers file not found: {p}")
    data = tomllib.loads(p.read_text())
    out: list[ProviderConfig] = []
    for raw in data.get("providers", []):
        raw = dict(raw)
        if "capabilities" in raw:
            raw["capabilities"] = set(raw["capabilities"])
        if "api_key" in raw:
            raise ValueError(f"provider {raw.get('id')}: put the key in an env var and use api_key_env")
        out.append(ProviderConfig(**raw))
    if not out:
        raise ValueError(f"no [[providers]] entries in {p}")
    return out


def build_adapter(cfg: ProviderConfig) -> ModelProviderAdapter:
    if cfg.adapter == "mock":
        from museos.gateway.mock import MockProvider

        return MockProvider(cfg)
    if cfg.adapter == "litellm":
        from museos.gateway.litellm_adapter import LiteLLMAdapter

        return LiteLLMAdapter(cfg)
    if cfg.adapter == "command":
        from museos.gateway.command import CommandAdapter

        return CommandAdapter(cfg)
    if cfg.adapter == "claude_cli":
        from museos.gateway.claude_cli import ClaudeCLIAdapter

        return ClaudeCLIAdapter(cfg)
    raise ValueError(f"unknown adapter {cfg.adapter!r}")


@dataclass
class _Health:
    consecutive_failures: int = 0
    opened_at: float | None = None
    total_calls: int = 0
    total_failures: int = 0


@dataclass
class ProviderRegistry:
    configs: dict[str, ProviderConfig]
    adapters: dict[str, ModelProviderAdapter]
    failure_threshold: int = 3
    cooldown_s: float = 30.0
    _health: dict[str, _Health] = field(default_factory=dict)

    @classmethod
    def from_configs(cls, configs: list[ProviderConfig], **kw: Any) -> "ProviderRegistry":
        cfgs = {c.id: c for c in configs}
        adapters = {c.id: build_adapter(c) for c in configs if c.enabled}
        return cls(configs=cfgs, adapters=adapters, **kw)

    def enabled_ids(self) -> list[str]:
        return [i for i, c in self.configs.items() if c.enabled and i in self.adapters]

    def is_healthy(self, provider_id: str) -> bool:
        h = self._health.get(provider_id)
        if not h or h.opened_at is None:
            return True
        if time.monotonic() - h.opened_at >= self.cooldown_s:
            h.opened_at = None  # half-open: allow a trial call
            h.consecutive_failures = 0
            return True
        return False

    def record_success(self, provider_id: str) -> None:
        h = self._health.setdefault(provider_id, _Health())
        h.total_calls += 1
        h.consecutive_failures = 0
        h.opened_at = None

    def record_failure(self, provider_id: str) -> None:
        h = self._health.setdefault(provider_id, _Health())
        h.total_calls += 1
        h.total_failures += 1
        h.consecutive_failures += 1
        if h.consecutive_failures >= self.failure_threshold:
            h.opened_at = time.monotonic()

    def semaphore(self, provider_id: str) -> asyncio.Semaphore:
        """One semaphore per provider for the whole process, so max_concurrency holds across runs."""
        sems = self.__dict__.setdefault("_semaphores", {})
        if provider_id not in sems:
            sems[provider_id] = asyncio.Semaphore(max(self.configs[provider_id].max_concurrency, 1))
        return sems[provider_id]

    def status(self) -> list[dict[str, Any]]:
        out = []
        for pid, cfg in self.configs.items():
            h = self._health.get(pid, _Health())
            view = cfg.public_view()
            view.update(healthy=self.is_healthy(pid), total_calls=h.total_calls,
                        total_failures=h.total_failures)
            out.append(view)
        return out
