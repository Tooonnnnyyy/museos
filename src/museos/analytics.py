"""GA4-style analytics over the event and ledger tables.

Everything is derived from rows in the store within a date window; nothing is
hardcoded, so the dashboard is identical for a fresh install (empty state), a
demo run, or a months-old database. No secrets pass through here: the tables
that carry keys (usage, attempts, decisions) hold provider ids and numbers only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from museos.blindtest import tally
from museos.store import AgentRunRow, ComparisonRow, EventRow, PolicyDecisionRow, RunRow, Store, UsageRow

VALID_RANGES = (7, 28, 90, 365)
_DEFAULT_RANGE = 28


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite returns naive datetimes


def _median(values: list[float]) -> float | None:
    return float(median(values)) if values else None


def window_from_days(days: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    """``[start, end)`` for the last ``days`` UTC days, ending at the start of the current UTC day."""
    end = (now or datetime.now(UTC)).replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(days=days), end


def _overview(runs: list[RunRow], attempts: list[AgentRunRow], usage: list[UsageRow],
              comparisons: list[ComparisonRow]) -> dict[str, Any]:
    statuses = Counter(r.status for r in runs)
    done = statuses["completed"] + statuses.get("partial", 0)
    latencies = [a.latency_ms for a in attempts]
    council = tally(comparisons)
    return {
        "runs": len(runs),
        "completed": done,
        "failed": statuses["failed"] + statuses.get("cancelled", 0),
        "completion_rate": (done / len(runs)) if runs else None,
        "spend_usd": round(sum(u.cost_usd for u in usage), 4),
        "model_calls": len(attempts),
        "failed_calls": sum(a.status != "completed" for a in attempts),
        "tokens": sum(u.input_tokens + u.output_tokens for u in usage),
        "p50_latency_ms": int(_median(latencies) or 0),
        "council": {
            "votes": council["voted"], "wins": council["museos_wins"],
            "losses": council["baseline_wins"], "ties": council["ties"],
            "win_rate": (council["museos_wins"] / council["voted"]) if council["voted"] else None,
            "verdict": council["verdict"],
        },
    }


async def summary(store: Store, days: int | None = None) -> dict[str, Any]:
    days = days if days in VALID_RANGES else _DEFAULT_RANGE
    since, until = window_from_days(days)
    prev_since = since - timedelta(days=days)

    runs = await store.runs_between(since, until)
    attempts = await store.rows_between(AgentRunRow, since, until)
    usage = await store.rows_between(UsageRow, since, until)
    decisions = await store.rows_between(PolicyDecisionRow, since, until)
    events = await store.events_between(since, until)
    comparisons = await store.rows_between(ComparisonRow, since, until)

    prev_runs = await store.runs_between(prev_since, since)
    prev_attempts = await store.rows_between(AgentRunRow, prev_since, since)
    prev_usage = await store.rows_between(UsageRow, prev_since, since)
    prev_comparisons = await store.rows_between(ComparisonRow, prev_since, since)

    overview = _overview(runs, attempts, usage, comparisons)
    prev = _overview(prev_runs, prev_attempts, prev_usage, prev_comparisons)

    # --- daily series, gaps filled
    days_map = {(since + timedelta(days=i)).date(): i for i in range(days)}
    series = [{"date": d.isoformat(), "runs": 0, "cost_usd": 0.0, "calls": 0, "tokens": 0,
               "p50_latency_ms": None}
              for d in sorted(days_map)]
    for r in runs:
        idx = days_map.get(_aware(r.created_at).date())
        if idx is not None:
            series[idx]["runs"] += 1
    for u in usage:
        idx = days_map.get(_aware(u.created_at).date())
        if idx is not None:
            series[idx]["cost_usd"] = round(series[idx]["cost_usd"] + u.cost_usd, 4)
            series[idx]["tokens"] += u.input_tokens + u.output_tokens
            series[idx]["calls"] += 1
    per_day_lat: dict[int, list[int]] = defaultdict(list)
    for a in attempts:
        idx = days_map.get(_aware(a.created_at).date())
        if idx is not None:
            per_day_lat[idx].append(a.latency_ms)
    for idx, vals in per_day_lat.items():
        series[idx]["p50_latency_ms"] = int(_median(vals) or 0)

    # --- breakdowns
    by_provider: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "cost_usd": 0.0, "tokens": 0, "errors": 0, "latency_ms": []})
    for a in attempts:
        p = by_provider[a.provider]
        p["calls"] += 1
        p["errors"] += a.status != "completed"
        p["latency_ms"].append(a.latency_ms)
    for u in usage:
        p = by_provider[u.provider]
        p["cost_usd"] += u.cost_usd
        p["tokens"] += u.input_tokens + u.output_tokens
    providers = []
    for pid, p in sorted(by_provider.items(), key=lambda kv: -kv[1]["cost_usd"]):
        providers.append({
            "provider": pid, "calls": p["calls"], "cost_usd": round(p["cost_usd"], 4),
            "tokens": p["tokens"], "errors": p["errors"],
            "error_rate": (p["errors"] / p["calls"]) if p["calls"] else None,
            "p50_latency_ms": int(_median(p["latency_ms"]) or 0),
        })

    by_role: dict[str, dict[str, Any]] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0})
    actor_by_task: dict[str, str] = {}
    for a in attempts:
        by_role[a.actor]["calls"] += 1
        actor_by_task.setdefault(a.task_key, a.actor)
    for u in usage:  # usage is joined to a role through the actor of the same task_key
        by_role[actor_by_task.get(u.task_key, "synthesis")]["cost_usd"] += u.cost_usd
    by_role = [{"role": k, "calls": v["calls"], "cost_usd": round(v["cost_usd"], 4)}
               for k, v in sorted(by_role.items(), key=lambda kv: -kv[1]["calls"])]

    # --- funnel across runs: planned -> executed -> reviewed -> synthesized
    planned = sum(1 for r in runs if r.plan)
    executed = len({a.run_id for a in attempts})
    reviewed = len({e.run_id for e in events if e.event_type == "review.completed"})
    synthesized = sum(1 for r in runs if r.deliverable)
    stages = [
        {"stage": "created", "runs": len(runs)},
        {"stage": "planned", "runs": planned},
        {"stage": "executed", "runs": executed},
        {"stage": "reviewed", "runs": reviewed},
        {"stage": "synthesized", "runs": synthesized},
    ]

    denied = [d for d in decisions if d.decision != "ALLOW"]

    def _delta(cur: Any, old: Any) -> float | None:
        if old in (0, None) or cur is None:
            return None
        return round((cur - old) / old, 4)

    return {
        "range": {"days": days, "start": since.date().isoformat(), "end": until.date().isoformat(),
                  "timezone": "UTC"},
        "overview": overview,
        "delta": {
            "runs": _delta(overview["runs"], prev["runs"]),
            "completed": _delta(overview["completed"], prev["completed"]),
            "spend_usd": _delta(overview["spend_usd"], prev["spend_usd"]),
            "model_calls": _delta(overview["model_calls"], prev["model_calls"]),
            "council_votes": _delta(overview["council"]["votes"], prev["council"]["votes"]),
        },
        "series": series,
        "breakdowns": {"by_provider": providers, "by_role": by_role,
                       "by_status": dict(statuses)},
        "stages": stages,
        "denials": {"total": len(denied), "by_rule": dict(Counter(d.rule for d in denied))},
        "receipts": [{"run_id": r.id, "idea": r.idea[:160], "status": r.status,
                      "cost_usd": r.spent_usd, "created_at": _aware(r.created_at).isoformat()}
                     for r in sorted(runs, key=lambda r: r.created_at, reverse=True)],
        "empty": not runs and not attempts,
    }
