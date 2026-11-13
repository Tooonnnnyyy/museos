"""Decision Receipt: one run, fully accounted for.

A run's *product* is the deliverable; its *receipt* is the proof: who spoke, what
each call cost, which policy decisions were made, and what failed. The receipt is
built from the ledger tables (tasks, agent_runs, usage_records, policy_decisions,
events), never from a prompt or a key. Both the JSON form and a deterministic
Markdown rendering are exposed, so any user can archive or share the evidence.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from statistics import median
from typing import Any

from museos.store import (AgentRunRow, ComparisonRow, EventRow, PolicyDecisionRow, RunRow, TaskRow, UsageRow)


def _aware(dt: datetime) -> str:
    if dt is None:
        return ""
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def build_receipt(run: RunRow, tasks: list[TaskRow], attempts: list[AgentRunRow], usage: list[UsageRow],
                  decisions: list[PolicyDecisionRow], events: list[EventRow],
                  comparison: ComparisonRow | None) -> dict[str, Any]:
    request = run.request or {}
    provider_calls: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "cost_usd": 0.0, "tokens": 0, "errors": 0, "latency_ms": []})
    actor_by_task: dict[str, str] = {}
    for a in attempts:
        p = provider_calls[a.provider]
        p["calls"] += 1
        p["errors"] += a.status != "completed"
        p["latency_ms"].append(a.latency_ms)
        actor_by_task.setdefault(a.task_key, a.actor)
    for u in usage:
        provider_calls[u.provider]["cost_usd"] += u.cost_usd
        provider_calls[u.provider]["tokens"] += u.input_tokens + u.output_tokens

    providers = []
    for pid, p in sorted(provider_calls.items(), key=lambda kv: -kv[1]["cost_usd"]):
        providers.append({
            "provider": pid, "calls": p["calls"], "cost_usd": round(p["cost_usd"], 4),
            "tokens": p["tokens"], "errors": p["errors"],
            "p50_latency_ms": int(median(p["latency_ms"]) if p["latency_ms"] else 0),
        })

    reviews = [{"target": t.task_key, "provider": t.provider, "status": t.status, "error": t.error}
               for t in tasks if t.kind == "review"]

    deliverable = run.deliverable or {}
    disagreements = deliverable.get("disagreements") or []
    notes = list(deliverable.get("limitations") or [])
    if comparison is not None:
        notes.append(f"blind test: baseline {comparison.baseline_status}")

    done = sum(1 for t in tasks if t.kind == "task" and t.status == "completed")
    total_tasks = sum(1 for t in tasks if t.kind == "task")

    return {
        "run_id": run.id,
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "status": run.status,
        "idea": run.idea,
        "started_at": _aware(run.created_at),
        "finished_at": _aware(run.updated_at),
        "generated_at": _aware(datetime.now(UTC)),
        "request": {
            "mode": request.get("mode", "dynamic"),
            "clarify": bool(request.get("clarify")),
            "cross_review": bool(request.get("cross_review")),
            "compare": bool(request.get("compare")),
            "budget_usd": request.get("budget_usd"),
            "max_tasks": request.get("max_tasks"),
            "concurrency": request.get("concurrency"),
            "language": request.get("language"),
        },
        "result": {
            "tasks_total": total_tasks,
            "tasks_completed": done,
            "tasks_failed": sum(1 for t in tasks if t.status == "failed"),
            "spent_usd": round(run.spent_usd, 4),
            "model_calls": len(attempts),
            "tokens_in": sum(u.input_tokens for u in usage),
            "tokens_out": sum(u.output_tokens for u in usage),
            "synthesis_source": deliverable.get("synthesis_source"),
            "disagreement_count": len(disagreements),
        },
        "providers": providers,
        "reviews": reviews,
        "policy_decisions": [{"actor": d.actor, "action": d.action, "resource": d.resource,
                              "decision": d.decision, "rule": d.rule, "reason": d.reason}
                             for d in decisions],
        "failures": [{"task": a.task_key, "attempt": a.attempt, "provider": a.provider,
                      "error": a.error} for a in attempts if a.status != "completed"],
        "events": [{"type": e.event_type, "at": _aware(e.occurred_at)} for e in events],
        "notes": notes,
        "blind_test": None if comparison is None else {
            "comparison_id": comparison.id, "museos_slot": comparison.museos_slot,
            "baseline_status": comparison.baseline_status, "baseline_provider": comparison.baseline_provider,
            "baseline_cost_usd": comparison.baseline_cost_usd, "choice": comparison.choice,
        },
    }


def receipt_markdown(r: dict[str, Any]) -> str:
    """Deterministic Markdown rendering of a receipt. No HTML, no images: safe to archive or share."""
    res = r["result"]
    lines = [
        f"# Decision Receipt — `{r['run_id']}`",
        "",
        f"- **Status:** {r['status']}",
        f"- **Started:** {r['started_at'] or '—'} · **Finished:** {r['finished_at'] or '—'}",
        f"- **Workspace / project:** `{r['workspace_id']}` / `{r['project_id']}`",
        f"- **Generated:** {r['generated_at']}",
        "",
        "## Idea",
        "",
        r["idea"],
        "",
        "## Request",
        "",
        "| Mode | Clarify | Cross-review | Blind test | Budget | Max tasks | Concurrency | Language |",
        "|---|---|---|---|---|---|---|---|",
        f"| {r['request']['mode']} | {r['request']['clarify']} | {r['request']['cross_review']} "
        f"| {r['request']['compare']} | {r['request']['budget_usd']} | {r['request']['max_tasks']} "
        f"| {r['request']['concurrency']} | {r['request']['language']} |",
        "",
        "## Result",
        "",
        f"- **Tasks:** {res['tasks_completed']}/{res['tasks_total']} completed, {res['tasks_failed']} failed",
        f"- **Spend:** ${res['spent_usd']:.4f} · **Calls:** {res['model_calls']}",
        f"- **Tokens:** {res['tokens_in']} in / {res['tokens_out']} out",
        f"- **Synthesis source:** {res['synthesis_source']}",
        f"- **Disagreements surfaced:** {res['disagreement_count']}",
        "",
    ]
    if r["providers"]:
        lines += ["## Providers", "", "| Provider | Calls | Cost | Tokens | Errors | p50 latency |",
                  "|---|---|---|---|---|---|"]
        for p in r["providers"]:
            lines.append(f"| {p['provider']} | {p['calls']} | ${p['cost_usd']:.4f} | {p['tokens']} "
                         f"| {p['errors']} | {p['p50_latency_ms']} ms |")
        lines += [""]
    if r["reviews"]:
        lines += ["## Cross-review", "", "| Target | Reviewer | Status |", "|---|---|---|"]
        for v in r["reviews"]:
            lines.append(f"| {v['target']} | {v['provider'] or '—'} | {v['status']} |")
        lines += [""]
    if r["policy_decisions"]:
        lines += ["## Policy decisions", "", "| Actor | Action | Decision | Rule | Reason |",
                  "|---|---|---|---|---|"]
        for d in r["policy_decisions"]:
            lines.append(f"| {d['actor']} | `{d['action']}` | {d['decision']} | {d['rule']} | {d['reason']} |")
        lines += [""]
    if r["failures"]:
        lines += ["## Failures", "", "| Task | Attempt | Provider | Error |", "|---|---|---|---|"]
        for f in r["failures"]:
            lines.append(f"| {f['task']} | {f['attempt']} | {f['provider']} | {f['error'] or '—'} |")
        lines += [""]
    if r["blind_test"]:
        b = r["blind_test"]
        lines += ["## Blind test", "", f"- **Comparison:** `{b['comparison_id']}` — MuseOS in slot {b['museos_slot']}",
                  f"- **Baseline:** {b['baseline_provider']} ({b['baseline_status']}), ${b['baseline_cost_usd']:.4f}",
                  f"- **Your vote:** {b['choice'] or 'pending'}", ""]
    if r["notes"]:
        lines += ["## Notes", ""] + [f"- {n}" for n in r["notes"]] + [""]
    return "\n".join(lines)
