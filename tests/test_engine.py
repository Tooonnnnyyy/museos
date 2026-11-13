"""End-to-end orchestration with mock providers (SQLite store)."""

from __future__ import annotations

import asyncio

from swarmos.contracts import CollaborationMode, RunRequest, RunStatus, TaskStatus
from swarmos.gateway.mock import MockProvider
from tests.conftest import mock_cfg

IDEA = "AI-powered pet social network with digital twins"


def three_providers(**overrides):
    base = {
        "alpha": dict(caps={"reasoning", "planning", "json", "synthesis"}),
        "beta": dict(caps={"reasoning", "json", "code", "review"}),
        "gamma": dict(caps={"reasoning", "json", "research", "review"}),
    }
    return [mock_cfg(pid, **{**kw, **overrides.get(pid, {})}) for pid, kw in base.items()]


async def test_happy_path_parallel_multi_provider_and_persisted(make_orch, store):
    # 0.5s replies so overlap does not depend on how fast the store writes between launches.
    slow = dict(latency_s=0.5)
    orch = make_orch(three_providers(alpha=slow, beta=slow, gamma=slow))
    d = await orch.run(RunRequest(idea=IDEA))
    assert d.status is RunStatus.completed
    assert len(d.sections) >= 3
    assert len(d.providers_used) >= 2, d.providers_used
    assert MockProvider.max_inflight >= 3, "independent tasks did not run concurrently"
    assert d.synthesis_source == "synthesis_llm"
    assert any("Mock providers" in x for x in d.limitations)
    assert d.evidence == []  # mocks never fabricate evidence

    tasks = await store.list_tasks(d.run_id)
    assert {t.status for t in tasks} == {"completed"}
    assert len(await store.list_usage(d.run_id)) >= len(tasks) + 2  # tasks + plan + synthesis
    assert all(p.decision == "ALLOW" for p in await store.list_policy_decisions(d.run_id))
    types = [e.event_type for e in await store.list_events(d.run_id)]
    assert types[0] == "run.created" and "run.plan.completed" in types and types[-1] == "synthesis.completed"
    run = await store.get_run(d.run_id)
    assert run.status == "completed" and run.spent_usd > 0


async def test_provider_timeout_falls_back_and_others_complete(make_orch, store):
    orch = make_orch(three_providers(beta=dict(hang=True)))
    d = await orch.run(RunRequest(idea=IDEA, task_timeout_s=0.3))
    assert d.status is RunStatus.completed
    tech = next(s for s in d.sections if s.task_id == "technical")
    assert tech.provider != "beta"
    attempts = await store.list_agent_runs(d.run_id)
    assert any(a.provider == "beta" and a.status == "timed_out" for a in attempts)
    assert any("fallback" in x for x in d.limitations)


async def test_failed_provider_does_not_erase_other_work(make_orch):
    # Fan-out pins every task copy to one provider with no fallback.
    orch = make_orch(three_providers(gamma=dict(always_fail=True, fail_retryable=False)))
    d = await orch.run(RunRequest(idea=IDEA, mode=CollaborationMode.fan_out, max_tasks=3))
    assert d.status is RunStatus.partial
    assert {s.provider for s in d.sections} == {"alpha", "beta"}
    assert len(d.failed_tasks) == 3 and all(f.task_id.endswith("__gamma") for f in d.failed_tasks)
    assert any("Partial result" in x for x in d.limitations)


async def test_retry_recovers_from_transient_failure(make_orch, store):
    orch = make_orch([mock_cfg("solo", fail_first_n=1)])
    d = await orch.run(RunRequest(idea=IDEA, max_tasks=2))
    assert d.status is RunStatus.completed
    attempts = await store.list_agent_runs(d.run_id)
    assert attempts[0].status == "failed" and attempts[1].status == "completed"


async def test_malformed_json_is_retried_then_falls_back(make_orch, store):
    orch = make_orch(three_providers(beta=dict(malformed=True)))
    d = await orch.run(RunRequest(idea=IDEA))
    assert d.status is RunStatus.completed
    bad = [a for a in await store.list_agent_runs(d.run_id) if a.provider == "beta"]
    assert bad and all(a.error.startswith("malformed_output") for a in bad)
    assert all(s.provider != "beta" for s in d.sections)


async def test_truncated_reply_retries_with_larger_cap(make_orch, store):
    # Reasoning models can spend the whole cap thinking; the retry must raise the cap, not repeat it.
    orch = make_orch([mock_cfg("only", truncate_below=6000)])
    d = await orch.run(RunRequest(idea=IDEA, max_tasks=2))
    assert d.status is RunStatus.completed
    runs = await store.list_agent_runs(d.run_id)
    truncated = [a for a in runs if a.error and a.error.startswith("truncated_output")]
    assert truncated and all("max_tokens=4000" in a.error for a in truncated)
    assert all(a.status == "completed" for a in runs if a.attempt == 2)
    assert orch.registry.is_healthy("only")  # truncation is not counted as a provider failure


async def test_single_malformed_provider_yields_failed_run_with_template_plan(make_orch):
    orch = make_orch([mock_cfg("only", malformed=True)])
    d = await orch.run(RunRequest(idea=IDEA))
    assert d.status is RunStatus.failed
    assert d.sections == [] and d.failed_tasks
    assert any("fixed template" in x for x in d.limitations)
    assert d.synthesis_source == "deterministic"


async def test_budget_limit_denies_calls(make_orch, store):
    orch = make_orch(three_providers())
    d = await orch.run(RunRequest(idea=IDEA, budget_usd=0.0005))
    run = await store.get_run(d.run_id)
    assert run.spent_usd <= 0.0005 + 1e-9
    assert d.status in (RunStatus.failed, RunStatus.partial)
    decisions = await store.list_policy_decisions(d.run_id)
    assert any(p.decision == "DENY" and p.rule == "budget" for p in decisions)
    assert "budget.threshold_reached" in [e.event_type for e in await store.list_events(d.run_id)]


async def test_max_steps_limit(make_orch, store):
    orch = make_orch(three_providers())
    d = await orch.run(RunRequest(idea=IDEA, max_steps=3))
    assert len(await store.list_agent_runs(d.run_id)) == 3
    assert any(p.rule == "max_steps" for p in await store.list_policy_decisions(d.run_id))


async def test_cancel_while_tasks_running(make_orch, store):
    orch = make_orch([mock_cfg("a", latency_s=0.1), mock_cfg("b", latency_s=120.0)])
    run_id, _ = await orch.submit(RunRequest(idea=IDEA, max_tasks=3))
    # Wait on real progress with a wall-clock limit, so a slow machine cannot change the outcome:
    # one task done, another still held by "b" (which only cancel can end).
    deadline = asyncio.get_event_loop().time() + 60
    while asyncio.get_event_loop().time() < deadline:
        statuses = {t.status for t in await store.list_tasks(run_id)}
        if "completed" in statuses and "running" in statuses:
            break
        await asyncio.sleep(0.02)
    assert orch.cancel(run_id)
    await orch.wait(run_id)
    row = await store.get_run(run_id)
    assert row.status == "cancelled"
    statuses = {t.task_key: t.status for t in await store.list_tasks(run_id)}
    assert "completed" in statuses.values() and "cancelled" in statuses.values()
    assert row.deliverable["sections"], "completed work must survive cancellation"


async def test_cross_review_is_bounded_and_independent(make_orch):
    orch = make_orch(three_providers())
    d = await orch.run(RunRequest(idea=IDEA, cross_review=True))
    authors = {s.task_id: s.provider for s in d.sections}
    reviewed = [r for r in d.reviews if r.status is TaskStatus.completed]
    assert reviewed and len(d.reviews) == len([s for s in d.sections if s.role != "critic"])
    for r in reviewed:
        assert r.reviewer_provider != authors[r.target_task_id]


async def test_manual_provider_restriction(make_orch):
    orch = make_orch(three_providers())
    d = await orch.run(RunRequest(idea=IDEA, mode=CollaborationMode.manual, providers=["gamma"]))
    assert d.providers_used == ["gamma"]


async def test_duplicate_submission_is_idempotent(make_orch, store):
    orch = make_orch(three_providers())
    req = RunRequest(idea=IDEA, max_tasks=2)
    r1, c1 = await orch.submit(req, idempotency_key="evt-123")
    r2, c2 = await orch.submit(req, idempotency_key="evt-123")
    assert r1 == r2 and c1 and not c2
    await orch.wait(r1)
    assert len(await store.list_tasks(r1)) == 2  # no duplicated tasks
