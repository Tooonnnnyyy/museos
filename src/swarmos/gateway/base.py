"""Provider-neutral model gateway contract.

Every model call in SwarmAgentOS goes through a ``ModelProviderAdapter``. Agents never
see credentials: adapters resolve the secret reference (an env var name) at call
time and never place it in prompts, logs, or the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

Purpose = Literal["plan", "task", "review", "synthesis", "health"]


class CompletionRequest(BaseModel):
    model: str
    messages: list[dict[str, str]]
    max_tokens: int = 1500
    temperature: float = 0.3
    json_mode: bool = True
    # Internal routing metadata. Never sent to a real provider.
    metadata: dict[str, Any] = Field(default_factory=dict)


class CompletionResponse(BaseModel):
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: int = 0
    finish_reason: str | None = None  # "length" means the output hit max_tokens


class ProviderError(Exception):
    """A provider call failed. ``retryable`` drives retry vs. fallback."""

    def __init__(self, message: str, *, retryable: bool, kind: str = "provider_error"):
        super().__init__(message)
        self.retryable = retryable
        self.kind = kind


class ModelProviderAdapter(Protocol):
    provider_id: str

    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...


@dataclass
class ProviderConfig:
    """Registry record for one provider. Holds a secret *reference*, never a secret."""

    id: str
    adapter: Literal["mock", "litellm", "claude_cli", "command"]
    model: str
    display_name: str = ""
    enabled: bool = True
    capabilities: set[str] = field(default_factory=lambda: {"reasoning", "planning", "json"})
    # USD per 1K tokens. None = unknown (budget pre-checks then use max_cost_usd).
    cost_per_1k_input: float | None = None
    cost_per_1k_output: float | None = None
    context_window: int | None = None
    api_key_env: str | None = None  # name of env var holding the key
    api_base: str | None = None
    supports_json_mode: bool = True
    max_concurrency: int = 4
    # Call caps (retries included). Used for subscription-backed providers to keep usage ordinary.
    max_calls_per_run: int | None = None
    max_calls_per_day: int | None = None
    region: str | None = None
    command: list[str] = field(default_factory=list)  # adapter 'command': argv; prompt on stdin, reply on stdout
    mock: dict[str, Any] = field(default_factory=dict)  # MockBehavior kwargs

    def public_view(self) -> dict[str, Any]:
        """Safe-to-expose description (no secret values)."""
        return {
            "id": self.id,
            "display_name": self.display_name or self.id,
            "adapter": self.adapter,
            "subscription": self.adapter in ("claude_cli", "command"),
            "model": self.model,
            "enabled": self.enabled,
            "capabilities": sorted(self.capabilities),
            "cost_per_1k_input": self.cost_per_1k_input,
            "cost_per_1k_output": self.cost_per_1k_output,
            "context_window": self.context_window,
            "credential_ref": self.api_key_env,
            "region": self.region,
            "max_concurrency": self.max_concurrency,
            "max_calls_per_run": self.max_calls_per_run,
            "max_calls_per_day": self.max_calls_per_day,
        }

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float | None:
        if self.cost_per_1k_input is None or self.cost_per_1k_output is None:
            return None
        return (input_tokens * self.cost_per_1k_input + output_tokens * self.cost_per_1k_output) / 1000
