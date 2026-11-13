"""Analytics summary and Decision Receipt, driven from a seeded SQLite store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from museos.analytics import summary
from museos.receipt import build_receipt, receipt_markdown
from museos.store import (AgentRunRow, ComparisonRow, EventRow, PolicyDecisionRow, RunRow, TaskRow, UsageRow)


def _t(days_ago: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days_ago)


@pytest.fixture
async def seeded(store):
    r1 = RunRow(id="a" * 32, workspace_id="w", project_id="p", idea="design a tea app", status="completed",
                request={"mode": "dynamic", "cross_review": True, "budget_usd": 0.5},
                plan={"tasks": [{"task_id": "research"}]}, deliverable={"synthesis_source": "llm",
                                                                      "disagreements": ["x vs y"]},
                spent_usd=0.12, created_at=_t(1), updated_at=_t(1))
    r2 = RunRow(id="b" * 32, workspace_id="w", project_id="p", idea="failing idea", status="failed",
                request={"mode": "dynamic"}, error="boom", created_at=_t(2), updated_at=_t(2))
    await store.add(r1)
    await store.add(r2)

    for i, (task_key, status, provider, latency) in enumerate([
        ("research", "completed", "p1", 400), ("critic", "completed", "p2", 700), ("research", "failed", "p1", 900),
    ]):
        await store.add(AgentRunRow(run_id="a" * 32, task_key=task_key, attempt=1, actor="worker",
                                    provider=provider, model=f"{provider}-m", status=status, latency_ms=latency,
                                    created_at=_t(1)))

    for cost, tin, tout, provider, task_key in [(0.05, 100, 50, "p1", "research"),
                                                (0.04, 80, 40, "p2", "critic"), (0.03, 60, 30, "p1", "research")]:
        await store.add(UsageRow(run_id="a" * 32, task_key=task_key, provider=provider, model=f"{provider}-m",
                                 input_tokens=tin, output_tokens=tout, cost_usd=cost, created_at=_t(1)))

    await store.add(TaskRow(run_id="a" * 32, task_key="research", kind="task", role="research_analyst",
                            spec={}, status="completed", provider="p1", model="p1-m"))
    await store.add(TaskRow(run_id="a" * 32, task_key="critic", kind="task", role="critic",
                            spec={}, status="completed", provider="p2", model="p2-m"))
    await store.add(TaskRow(run_id="a" * 32, task_key="review__research", kind="review", role="cross_reviewer",
                            spec={}, status="completed", provider="p2", model="p2-m"))

    await store.add(PolicyDecisionRow(run_id="a" * 32, actor="worker", action="model.complete",
                                      decision="DENY", rule="budget", reason="over budget", created_at=_t(1)))
    for et in ("run.plan.completed", "review.completed", "synthesis.completed"):
        await store.add(EventRow(seq=1, event_type=et, workspace_id="w", project_id="p", run_id="a" * 32,
                                 correlation_id="a" * 32, payload={}, occurred_at=_t(1)))
    await store.add(ComparisonRow(run_id="a" * 32, museos_slot="A", baseline_status="completed",
                                  choice="A", baseline_cost_usd=0.02, created_at=_t(1)))
    yield store


async def test_summary_overview_and_series(seeded):
    d = await summary(seeded, 28)
    o = d["overview"]
    assert o["runs"] == 2
    assert o["model_calls"] == 3
    assert o["failed_calls"] == 1
    assert o["spend_usd"] == round(0.05 + 0.04 + 0.03, 4)
    assert o["completion_rate"] == 0.5
    assert o["tokens"] == (100 + 50 + 80 + 40 + 60 + 30)
    assert o["p50_latency_ms"] == 700  # median of 400, 700, 900
    assert o["council"]["votes"] == 1 and o["council"]["wins"] == 1 and o["council"]["win_rate"] == 1.0
    assert len(d["series"]) == 28
    assert sum(s["runs"] for s in d["series"]) == 2
    assert round(sum(s["cost_usd"] for s in d["series"]), 4) == o["spend_usd"]


async def test_summary_breakdowns_and_funnel(seeded):
    d = await summary(seeded, 28)
    by_provider = {p["provider"]: p for p in d["breakdowns"]["by_provider"]}
    assert by_provider["p1"]["calls"] == 2 and by_provider["p1"]["errors"] == 1
    assert by_provider["p2"]["calls"] == 1 and by_provider["p2"]["errors"] == 0
    stages = {s["stage"]: s["runs"] for s in d["stages"]}
    assert stages["created"] == 2 and stages["planned"] == 1 and stages["reviewed"] == 1
    assert stages["synthesized"] == 1
    assert d["denials"]["total"] == 1 and d["denials"]["by_rule"] == {"budget": 1}
    assert d["empty"] is False


async def test_summary_empty_state(store):
    d = await summary(store, 7)
    assert d["empty"] is True
    assert d["overview"]["runs"] == 0
    assert d["overview"]["council"]["win_rate"] is None
    assert len(d["series"]) == 7


async def test_receipt_and_markdown(seeded):
    run = await seeded.get_run("a" * 32)
    receipt = build_receipt(
        run, await seeded.list_tasks("a" * 32), await seeded.list_agent_runs("a" * 32),
        await seeded.list_usage("a" * 32), await seeded.list_policy_decisions("a" * 32),
        await seeded.list_events("a" * 32), await seeded.get_comparison_by_run("a" * 32))
    assert receipt["result"]["tasks_total"] == 2 and receipt["result"]["tasks_completed"] == 2
    assert receipt["result"]["disagreement_count"] == 1
    assert receipt["providers"][0]["provider"] == "p1"
    assert len(receipt["reviews"]) == 1
    assert receipt["policy_decisions"][0]["rule"] == "budget"
    assert receipt["blind_test"]["choice"] == "A"

    md = receipt_markdown(receipt)
    assert md.startswith("# Decision Receipt")
    assert "design a tea app" in md
    assert "| p1 |" in md and "budget" in md
    assert "No secrets" not in md  # sanity: receipts carry no key material by construction
