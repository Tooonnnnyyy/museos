"""Dynamic model router: picks a provider per task and explains why.

Roles are never permanently bound to providers. Selection weighs capability
match, user preference/exclusion, provider health, cost, and how many tasks a
provider already has in this run (spreading work gives independent opinions).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from swarmos.contracts import TaskSpec
from swarmos.gateway.registry import ProviderRegistry


@dataclass
class RouteDecision:
    provider_id: str
    model: str
    reason: str
    fallbacks: list[str] = field(default_factory=list)
    expected_cost_usd: float | None = None
    unsupported: list[str] = field(default_factory=list)


class ModelRouter:
    def __init__(self, registry: ProviderRegistry, allowed: list[str] | None = None):
        self.registry = registry
        enabled = registry.enabled_ids()
        self.allowed = [p for p in enabled if allowed is None or p in allowed]
        self.load: dict[str, int] = defaultdict(int)

    def _rank(self, caps: list[str], preferred: list[str], excluded: list[str],
              exclude_also: tuple[str, ...] = ()) -> list[tuple[float, str, list[str]]]:
        ranked = []
        for pid in self.allowed:
            if pid in excluded or pid in exclude_also or not self.registry.is_healthy(pid):
                continue
            cfg = self.registry.configs[pid]
            missing = [c for c in caps if c not in cfg.capabilities]
            score = 3.0 * (len(caps) - len(missing))
            if pid in preferred:
                score += 5.0
            score -= 2.0 * self.load[pid]
            if cfg.cost_per_1k_output is not None:
                score -= min(cfg.cost_per_1k_output * 10, 1.0)
            ranked.append((score, pid, missing))
        ranked.sort(key=lambda r: (-r[0], self.allowed.index(r[1])))
        return ranked

    def route(self, caps: list[str], preferred: list[str] | None = None, excluded: list[str] | None = None,
              exclude_also: tuple[str, ...] = (), est_tokens: tuple[int, int] = (0, 0),
              count_load: bool = True) -> RouteDecision | None:
        ranked = self._rank(caps, preferred or [], excluded or [], exclude_also)
        if not ranked:
            return None
        score, pid, missing = ranked[0]
        cfg = self.registry.configs[pid]
        if count_load:
            self.load[pid] += 1
        parts = [f"score={score:.1f}"]
        if caps:
            parts.append(f"capabilities {len(caps) - len(missing)}/{len(caps)} matched")
        if preferred and pid in preferred:
            parts.append("user-preferred")
        parts.append(f"load before assignment={self.load[pid] - (1 if count_load else 0)}")
        return RouteDecision(
            provider_id=pid,
            model=cfg.model,
            reason="; ".join(parts),
            fallbacks=[r[1] for r in ranked[1:]],
            expected_cost_usd=cfg.estimate_cost(*est_tokens),
            unsupported=missing,
        )

    def route_task(self, task: TaskSpec) -> RouteDecision | None:
        return self.route(task.required_capabilities, task.preferred_providers, task.excluded_providers,
                          est_tokens=(1000, task.max_output_tokens))
