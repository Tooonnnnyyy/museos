"""Run-level orchestration graph (LangGraph, ADR-002).

    plan -> execute -> [review] -> synthesize -> END

LangGraph owns the run-level state machine and checkpoints each step; the
dynamic task DAG inside ``execute`` is run by ``DAGScheduler`` because the
number and shape of tasks is decided at runtime by the Chief.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from museos.chief import make_plan
from museos.config import Settings
from museos.contracts import (AgentResult, BriefAnswer, ClarifyOutput, Deliverable, Plan, ReviewResult, RunRequest,
                              RunStatus, TaskSpec, TaskStatus)
from museos.engine import RunContext, call_model, execute_task
from museos.gateway.registry import ProviderRegistry
from museos.policy import Budget, PolicyConfig, PolicyEngine
from museos.router import ModelRouter
from museos.scheduler import DAGScheduler
from museos.prompts import baseline_messages, build_brief, clarify_messages, extract_json
from museos.store import ComparisonRow, Store
from museos.synthesis import cross_review, synthesize

log = logging.getLogger("museos")


class RunState(TypedDict, total=False):
    run_id: str
    plan: dict[str, Any]
    results: list[dict[str, Any]]
    reviews: list[dict[str, Any]]
    deliverable: dict[str, Any]


class Orchestrator:
    def __init__(self, settings: Settings, registry: ProviderRegistry, store: Store):
        self.settings = settings
        self.registry = registry
        self.store = store
        self.policy = PolicyEngine(PolicyConfig(external_writes_enabled=settings.external_writes_enabled,
                                                destination_allowlist=settings.destination_allowlist))
        self.checkpointer = InMemorySaver()
        self._cancel: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._side_tasks: dict[str, asyncio.Task] = {}  # single-model baselines for blind tests

    # ---------------------------------------------------------------- lifecycle
    async def submit(self, request: RunRequest, idempotency_key: str | None = None) -> tuple[str, bool]:
        row, created = await self.store.create_run(
            idea=request.idea, request=request.model_dump(mode="json"), workspace_id=request.workspace_id,
            project_id=request.project_id, idempotency_key=idempotency_key)
        if created:
            self._cancel[row.id] = asyncio.Event()
            await self.store.event(row.id, "run.created", {"mode": request.mode.value},
                                   workspace_id=request.workspace_id, project_id=request.project_id)
            if request.compare:
                await self.store.add(ComparisonRow(run_id=row.id, museos_slot=random.choice("AB")))
            if request.clarify:
                self._tasks[row.id] = asyncio.create_task(self._clarify_guarded(row.id, request))
            else:
                self._launch(row.id, request, request.idea)
        return row.id, created

    def _launch(self, run_id: str, request: RunRequest, brief: str) -> None:
        """Start the run, and the single-model baseline if this is a blind test, on the same brief."""
        briefed = request.model_copy(update={"idea": brief})
        self._cancel.setdefault(run_id, asyncio.Event())
        self._tasks[run_id] = asyncio.create_task(self._run_guarded(run_id, briefed))
        if request.compare:
            self._side_tasks[run_id] = asyncio.create_task(self._baseline_guarded(run_id, briefed))

    async def answer(self, run_id: str, answers: list[str], skip: bool = False) -> bool:
        """Resume a run that is waiting on the owner's answers. Reads its state from the database,
        so it also works after a process restart."""
        # Atomic claim: only one caller can move the run out of awaiting_input, so it starts once.
        if not await self.store.claim_run(run_id, RunStatus.awaiting_input.value, RunStatus.queued.value):
            return False
        row = await self.store.get_run(run_id)
        ev = await self.store.find_event(run_id, "run.clarification.requested")
        c = ClarifyOutput.model_validate(ev.payload) if ev else ClarifyOutput(goal=row.idea)
        request = RunRequest.model_validate(row.request)
        brief = build_brief(row.idea, c.goal, c.questions, [] if skip else answers, c.assumptions)
        await self.store.event(run_id, "run.clarification.answered",
                               {"answered": 0 if skip else sum(1 for a in answers if a.strip()), "skipped": skip,
                                "brief": brief},
                               workspace_id=request.workspace_id, project_id=request.project_id)
        self._launch(run_id, request, brief)
        return True

    async def cancel_waiting(self, run_id: str) -> bool:
        return await self.store.claim_run(run_id, RunStatus.awaiting_input.value, RunStatus.cancelled.value)

    async def wait(self, run_id: str) -> None:
        for tasks in (self._tasks, self._side_tasks):
            t = tasks.get(run_id)
            if t:
                await asyncio.shield(t)

    def cancel(self, run_id: str) -> bool:
        ev = self._cancel.get(run_id)
        if ev is None or (run_id in self._tasks and self._tasks[run_id].done()):
            return False
        ev.set()
        return True

    async def shutdown(self) -> None:
        for ev in self._cancel.values():
            ev.set()
        pending = [*self._tasks.values(), *self._side_tasks.values()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def run(self, request: RunRequest) -> Deliverable:
        run_id, _ = await self.submit(request)
        await self.wait(run_id)
        row = await self.store.get_run(run_id)
        if row is None or row.deliverable is None:
            raise RuntimeError(f"run {run_id} produced no deliverable: {row.error if row else 'missing'}")
        return Deliverable.model_validate(row.deliverable)

    async def _run_guarded(self, run_id: str, request: RunRequest) -> None:
        try:
            await self._execute(run_id, request)
        except Exception as e:  # never leave a run stuck in "running"
            log.exception("run %s failed", run_id)
            await self.store.update_run(run_id, status=RunStatus.failed.value, error=f"{type(e).__name__}: {e}")
            await self.store.event(run_id, "run.failed", {"error": type(e).__name__},
                                   workspace_id=request.workspace_id, project_id=request.project_id)

    def _context(self, run_id: str, request: RunRequest, *, allowed: list[str] | None = None,
                 spent_usd: float = 0.0) -> RunContext:
        ctx = RunContext(
            run_id=run_id, request=request, registry=self.registry,
            router=ModelRouter(self.registry, allowed=allowed or request.providers),
            policy=self.policy, budget=Budget(request.budget_usd, request.max_steps), store=self.store,
            cancel_event=self._cancel.setdefault(run_id, asyncio.Event()),
            retry_backoff_s=self.settings.retry_backoff_s,
        )
        ctx.budget.spent_usd = spent_usd
        if not ctx.router.allowed:
            raise RuntimeError("no enabled providers match the request")
        return ctx

    # ---------------------------------------------------------------- clarify before dispatch
    async def _clarify_guarded(self, run_id: str, request: RunRequest) -> None:
        try:
            await self._clarify(run_id, request)
        except Exception as e:
            log.exception("clarify for run %s failed", run_id)
            await self.store.update_run(run_id, status=RunStatus.failed.value, error=f"{type(e).__name__}: {e}")

    async def _clarify(self, run_id: str, request: RunRequest) -> None:
        ctx = self._context(run_id, request)
        await self.store.update_run(run_id, status=RunStatus.clarifying.value)
        route = ctx.router.route(["planning"], count_load=False)
        value: ClarifyOutput | None = None
        if route is not None:
            outcome = await call_model(
                ctx, actor="chief", task_key="__clarify__", route=route, messages=clarify_messages(request.idea, request.language),
                parse=lambda text: ClarifyOutput.model_validate(extract_json(text)),
                max_tokens=2000, timeout_s=90, max_retries=1, max_fallbacks=1,
                max_cost_usd=min(0.25, request.budget_usd), metadata={"purpose": "clarify", "idea": request.idea})
            value = outcome.value
        await self.store.update_run(run_id, spent_usd=ctx.budget.spent_usd)
        if ctx.cancelled:
            await self.store.update_run(run_id, status=RunStatus.cancelled.value)
            return
        if value is None:  # never block a run on a failed clarification
            await ctx.emit("run.clarification.skipped", {"reason": "clarify call failed"})
            self._launch(run_id, request, request.idea)
            return
        if not value.questions:
            await ctx.emit("run.clarification.not_needed", {"goal": value.goal})
            self._launch(run_id, request, build_brief(request.idea, value.goal, [], [], value.assumptions))
            return
        await ctx.emit("run.clarification.requested", value.model_dump())
        await self.store.update_run(run_id, status=RunStatus.awaiting_input.value)

    # ---------------------------------------------------------------- blind-test baseline
    async def _baseline_guarded(self, run_id: str, request: RunRequest) -> None:
        cmp = await self.store.get_comparison_by_run(run_id)
        if cmp is None:
            return
        try:
            await self._baseline(run_id, request, cmp.id)
        except Exception as e:
            log.exception("baseline for run %s failed", run_id)
            await self.store.update_comparison(cmp.id, baseline_status="failed",
                                               baseline_error=f"{type(e).__name__}: {e}")

    async def _baseline(self, run_id: str, request: RunRequest, comparison_id: str) -> None:
        """One single model answers the same brief alone. It has its own budget, so it cannot starve the run."""
        pinned = [request.baseline_provider] if request.baseline_provider else None
        ctx = self._context(run_id, request, allowed=pinned)
        await self.store.update_comparison(comparison_id, baseline_status="running")
        route = ctx.router.route(["reasoning", "synthesis"], count_load=False)
        if route is None:
            raise RuntimeError("no healthy provider for the single-model baseline")
        started = time.perf_counter()
        outcome = await call_model(
            ctx, actor="baseline", task_key="__baseline__", route=route, messages=baseline_messages(request.idea, request.language),
            parse=lambda text: BriefAnswer.model_validate(extract_json(text)),
            max_tokens=6000, timeout_s=180, max_retries=1, max_fallbacks=0 if pinned else 1,
            max_cost_usd=min(0.5, request.budget_usd), metadata={"purpose": "baseline", "idea": request.idea})
        await self.store.update_comparison(
            comparison_id, baseline_status="completed" if outcome.value is not None else "failed",
            baseline=outcome.value.model_dump() if outcome.value is not None else None,
            baseline_provider=outcome.provider, baseline_model=outcome.model, baseline_error=outcome.error,
            baseline_cost_usd=ctx.budget.spent_usd, baseline_latency_ms=int((time.perf_counter() - started) * 1000))

    # ---------------------------------------------------------------- graph
    async def _execute(self, run_id: str, request: RunRequest) -> None:
        row = await self.store.get_run(run_id)
        ctx = self._context(run_id, request, spent_usd=row.spent_usd if row else 0.0)
        graph = self._build_graph(ctx)
        await graph.ainvoke({"run_id": run_id}, config={"configurable": {"thread_id": run_id}})

    def _build_graph(self, ctx: RunContext):
        store, request = self.store, ctx.request

        async def plan_node(state: RunState) -> RunState:
            await store.update_run(ctx.run_id, status=RunStatus.planning.value)
            plan = await make_plan(ctx, request)
            for t in plan.tasks:
                await store.upsert_task(ctx.run_id, t.task_id, role=t.role.value, spec=t.model_dump(),
                                        status=TaskStatus.blocked.value)
            await store.update_run(ctx.run_id, plan=plan.model_dump(mode="json"))
            await ctx.emit("run.plan.completed", {"source": plan.source, "tasks": len(plan.tasks),
                                                  "planner": plan.planner_provider})
            return {"plan": plan.model_dump(mode="json")}

        async def execute_node(state: RunState) -> RunState:
            plan = Plan.model_validate(state["plan"])
            await store.update_run(ctx.run_id, status=RunStatus.running.value)

            async def on_status(t: TaskSpec, status: TaskStatus, result: AgentResult | None) -> None:
                if status in (TaskStatus.ready, TaskStatus.skipped, TaskStatus.cancelled):
                    await store.upsert_task(ctx.run_id, t.task_id, role=t.role.value, spec=t.model_dump(),
                                            status=status.value, error=result.error if result else None,
                                            result=result.model_dump(mode="json") if result else None)
                    await ctx.emit(f"task.{status.value}", {}, task_id=t.task_id)

            async def run_one(t: TaskSpec, upstream: list[AgentResult]) -> AgentResult:
                try:
                    return await execute_task(ctx, plan.goal, t, upstream)
                except Exception as e:  # keep the task row truthful if anything unexpected escapes
                    log.exception("task %s crashed", t.task_id)
                    r = AgentResult(task_id=t.task_id, agent_role=t.role.value, status=TaskStatus.failed,
                                    error=f"internal error: {type(e).__name__}")
                    await store.upsert_task(ctx.run_id, t.task_id, role=t.role.value, spec=t.model_dump(),
                                            status=r.status.value, error=r.error, result=r.model_dump(mode="json"))
                    await ctx.emit("agent_run.failed", {"status": "failed", "error": r.error}, task_id=t.task_id)
                    return r

            sched = DAGScheduler(plan.tasks, run_one,
                                 concurrency=request.concurrency, cancel_event=ctx.cancel_event,
                                 on_status=on_status)
            results = await sched.run()
            ordered = [results[t.task_id] for t in plan.tasks]
            return {"results": [r.model_dump(mode="json") for r in ordered]}

        async def review_node(state: RunState) -> RunState:
            await store.update_run(ctx.run_id, status=RunStatus.reviewing.value)
            plan = Plan.model_validate(state["plan"])
            results = [AgentResult.model_validate(r) for r in state["results"]]
            reviews = await cross_review(ctx, plan.goal, results)
            return {"reviews": [r.model_dump(mode="json") for r in reviews]}

        async def synth_node(state: RunState) -> RunState:
            await store.update_run(ctx.run_id, status=RunStatus.synthesizing.value)
            plan = Plan.model_validate(state["plan"])
            results = [AgentResult.model_validate(r) for r in state["results"]]
            reviews = [ReviewResult.model_validate(r) for r in state.get("reviews", [])]
            d = await synthesize(ctx, plan, results, reviews)
            await store.update_run(ctx.run_id, status=d.status.value, deliverable=d.model_dump(mode="json"),
                                   spent_usd=ctx.budget.spent_usd)
            await ctx.emit("synthesis.completed", {"status": d.status.value, "source": d.synthesis_source})
            if d.status is RunStatus.cancelled:
                await ctx.emit("run.cancelled", {})
            return {"deliverable": d.model_dump(mode="json")}

        def after_execute(state: RunState) -> str:
            plan = state["plan"]
            if plan.get("requires_cross_review") and not ctx.cancelled:
                return "review"
            return "synthesize"

        g = StateGraph(RunState)
        g.add_node("plan", plan_node)
        g.add_node("execute", execute_node)
        g.add_node("review", review_node)
        g.add_node("synthesize", synth_node)
        g.add_edge(START, "plan")
        g.add_edge("plan", "execute")
        g.add_conditional_edges("execute", after_execute, {"review": "review", "synthesize": "synthesize"})
        g.add_edge("review", "synthesize")
        g.add_edge("synthesize", END)
        return g.compile(checkpointer=self.checkpointer)
