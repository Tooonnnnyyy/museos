"""Deterministic, server-side policy engine.

Model prompts are not an authorization boundary: every model call and every
tool/connector action is checked here first. No I/O, no randomness, so it is
fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class PolicyInput(BaseModel):
    actor: str
    workspace_id: str = "default"
    action: str  # e.g. "model.complete", "tool.web_search", "slack.post_message"
    resource: str | None = None
    risk_level: Literal["low", "medium", "high"] = "low"
    is_external_write: bool = False
    is_destructive: bool = False
    is_financial: bool = False
    is_public: bool = False
    budget_remaining_usd: float | None = None
    estimated_cost_usd: float | None = None
    steps_remaining: int | None = None
    tools_allowed: list[str] | None = None
    idempotency_key: str | None = None


class PolicyDecision(BaseModel):
    decision: Decision
    reason: str
    rule: str


DENIED_ACTIONS = frozenset({
    "secrets.export", "secrets.read", "shell.exec", "session.scrape", "credentials.read",
})


@dataclass
class PolicyConfig:
    # Kill switch: when False, all external writes are denied.
    external_writes_enabled: bool = False
    destination_allowlist: set[str] = field(default_factory=set)
    denied_actions: frozenset[str] = DENIED_ACTIONS


class PolicyEngine:
    def __init__(self, config: PolicyConfig | None = None):
        self.config = config or PolicyConfig()

    def decide(self, p: PolicyInput) -> PolicyDecision:
        c = self.config
        if p.action in c.denied_actions:
            return PolicyDecision(decision=Decision.DENY, reason=f"action {p.action} is denied by default",
                                  rule="deny_list")
        if p.action.startswith("tool."):
            tool = p.action.removeprefix("tool.")
            if p.tools_allowed is None or tool not in p.tools_allowed:
                return PolicyDecision(decision=Decision.DENY, reason=f"tool {tool} not in task allowlist",
                                      rule="tool_allowlist")
        if p.steps_remaining is not None and p.steps_remaining <= 0:
            return PolicyDecision(decision=Decision.DENY, reason="max steps reached", rule="max_steps")
        if p.budget_remaining_usd is not None and p.estimated_cost_usd is not None:
            if p.estimated_cost_usd > p.budget_remaining_usd + 1e-12:
                return PolicyDecision(
                    decision=Decision.DENY,
                    reason=f"estimated ${p.estimated_cost_usd:.4f} exceeds remaining ${p.budget_remaining_usd:.4f}",
                    rule="budget",
                )
        if p.is_financial or p.is_destructive or p.is_public:
            return PolicyDecision(decision=Decision.REQUIRE_APPROVAL,
                                  reason="financial/destructive/public actions need human approval",
                                  rule="tier3_approval")
        if p.is_external_write:
            if not c.external_writes_enabled:
                return PolicyDecision(decision=Decision.DENY, reason="external writes are disabled (kill switch)",
                                      rule="kill_switch")
            if not p.resource or p.resource not in c.destination_allowlist:
                return PolicyDecision(decision=Decision.DENY,
                                      reason=f"destination {p.resource!r} is not allowlisted",
                                      rule="destination_allowlist")
            if p.risk_level == "high":
                return PolicyDecision(decision=Decision.REQUIRE_APPROVAL, reason="high-risk external write",
                                      rule="high_risk_write")
        return PolicyDecision(decision=Decision.ALLOW, reason="within policy", rule="default_allow")


class Budget:
    """Per-run cost and step ceiling with reservations.

    asyncio is single-threaded, so check-then-reserve without an ``await`` in
    between is atomic.
    """

    def __init__(self, limit_usd: float, max_steps: int):
        self.limit_usd = limit_usd
        self.max_steps = max_steps
        self.spent_usd = 0.0
        self.reserved_usd = 0.0
        self.steps = 0
        self.unknown_cost_calls = 0

    @property
    def remaining_usd(self) -> float:
        return max(self.limit_usd - self.spent_usd - self.reserved_usd, 0.0)

    @property
    def steps_remaining(self) -> int:
        return self.max_steps - self.steps

    def reserve(self, amount: float) -> None:
        self.reserved_usd += amount
        self.steps += 1

    def settle(self, reserved: float, actual: float | None) -> float:
        """Release a reservation and book the actual cost. Unknown cost books the reservation."""
        self.reserved_usd = max(self.reserved_usd - reserved, 0.0)
        if actual is None:
            self.unknown_cost_calls += 1
            actual = reserved
        self.spent_usd += actual
        return actual
