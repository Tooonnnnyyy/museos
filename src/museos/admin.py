"""Read-only numbers for the admin page: usage, cost, provider health, failures, settings.

Nothing here returns a secret: keys are reported only as present or missing, and the database URL is
reduced to its kind.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from museos.blindtest import tally
from museos.config import Settings
from museos.gateway.registry import ProviderRegistry
from museos.store import AgentRunRow, PolicyDecisionRow, RunRow, Store, UsageRow

DAYS = 14


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite returns naive datetimes


async def summarize(store: Store, registry: ProviderRegistry, settings: Settings) -> dict[str, Any]:
    runs = await store.recent(RunRow, 2000)
    attempts = await store.recent(AgentRunRow)
    usage = await store.recent(UsageRow)
    decisions = await store.recent(PolicyDecisionRow)
    now = datetime.now(UTC)
    day_ago = now - timedelta(days=1)

    per: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "attempts": 0, "completed": 0, "failed": 0, "latency_ms": 0, "calls_24h": 0,
        "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
    for a in attempts:
        p = per[a.provider]
        p["attempts"] += 1
        p["completed" if a.status == "completed" else "failed"] += 1
        p["latency_ms"] += a.latency_ms
        p["calls_24h"] += _aware(a.created_at) >= day_ago
    for u in usage:
        p = per[u.provider]
        p["input_tokens"] += u.input_tokens
        p["output_tokens"] += u.output_tokens
        p["cost_usd"] += u.cost_usd

    providers = []
    for row in registry.status():
        cfg = registry.configs[row["id"]]
        s = per.pop(row["id"], None) or dict(per.default_factory())  # type: ignore[misc]
        providers.append({
            "id": cfg.id, "display_name": cfg.display_name or cfg.id, "adapter": cfg.adapter, "model": cfg.model,
            "enabled": cfg.enabled, "healthy": row["healthy"], "subscription": row["subscription"],
            "key": None if not cfg.api_key_env else ("set" if os.environ.get(cfg.api_key_env) else "missing"),
            "max_concurrency": cfg.max_concurrency, "max_calls_per_run": cfg.max_calls_per_run,
            "max_calls_per_day": cfg.max_calls_per_day,
            "avg_latency_ms": round(s["latency_ms"] / s["attempts"]) if s["attempts"] else 0,
            **{k: s[k] for k in ("attempts", "completed", "failed", "calls_24h", "input_tokens",
                                 "output_tokens", "cost_usd")},
        })
    # Providers that appear in history but are no longer configured.
    for pid, s in per.items():
        providers.append({"id": pid, "display_name": pid, "adapter": "removed", "model": "", "enabled": False,
                          "healthy": True, "subscription": False, "key": None, "max_concurrency": None,
                          "max_calls_per_run": None, "max_calls_per_day": None,
                          "avg_latency_ms": round(s["latency_ms"] / s["attempts"]) if s["attempts"] else 0,
                          **{k: s[k] for k in ("attempts", "completed", "failed", "calls_24h", "input_tokens",
                                               "output_tokens", "cost_usd")}})

    days = [(now - timedelta(days=i)).date().isoformat() for i in range(DAYS - 1, -1, -1)]
    by_day = {d: {"date": d, "runs": 0, "cost_usd": 0.0, "calls": 0} for d in days}
    for r in runs:
        d = _aware(r.created_at).date().isoformat()
        if d in by_day:
            by_day[d]["runs"] += 1
    for u in usage:
        d = _aware(u.created_at).date().isoformat()
        if d in by_day:
            by_day[d]["cost_usd"] += u.cost_usd
            by_day[d]["calls"] += 1

    try:
        ver = version("museos")
    except PackageNotFoundError:
        ver = "dev"
    denied = [d for d in decisions if d.decision != "ALLOW"]
    return {
        "totals": {
            "runs": len(runs), "by_status": dict(Counter(r.status for r in runs)),
            "model_calls": len(attempts), "failed_calls": sum(a.status != "completed" for a in attempts),
            "cost_usd": sum(u.cost_usd for u in usage),
            "tokens": sum(u.input_tokens + u.output_tokens for u in usage),
        },
        "providers": providers,
        "by_day": list(by_day.values()),
        "failures": [{"at": _aware(a.created_at).isoformat(), "run_id": a.run_id, "task": a.task_key,
                      "provider": a.provider, "status": a.status, "error": (a.error or "")[:200]}
                     for a in attempts if a.status != "completed"][:25],
        "denials": {"total": len(denied), "by_rule": dict(Counter(d.rule for d in denied))},
        "blind_test": tally(await store.list_comparisons(200)),
        "settings": {
            "version": ver,
            "database": "postgresql" if settings.database_url.startswith("postgres") else "sqlite",
            "providers_file": settings.providers_file or "(demo models)",
            "max_budget_usd": settings.max_budget_usd, "allowed_hosts": settings.allowed_hosts,
            "external_writes_enabled": settings.external_writes_enabled, "login": False,
        },
    }
