"""Run context and the single choke point for model calls.

``call_model`` enforces, for every call: policy check, budget/step reservation,
per-provider concurrency, timeout, bounded retries, provider fallback, health
tracking, output validation, and audit/usage records.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from pydantic import ValidationError

from museos.contracts import AgentResult, RunRequest, TaskSpec, TaskStatus, Usage
from museos.gateway.base import CompletionRequest, ProviderError
from museos.gateway.registry import ProviderRegistry
from museos.policy import Budget, Decision, PolicyEngine, PolicyInput
from museos.router import ModelRouter, RouteDecision
from museos.store import AgentRunRow, PolicyDecisionRow, Store, UsageRow

T = TypeVar("T")

MAX_OUTPUT_TOKENS_CEILING = 16000


@dataclass
class RunContext:
    run_id: str
    request: RunRequest
    registry: ProviderRegistry
    router: ModelRouter
    policy: PolicyEngine
    budget: Budget
    store: Store
    cancel_event: asyncio.Event
    retry_backoff_s: float = 0.25
    provider_semaphores: dict[str, asyncio.Semaphore] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # run-level limitations to surface
    provider_calls: dict[str, int] = field(default_factory=dict)  # attempts reserved this run
    provider_calls_before_run: dict[str, int] = field(default_factory=dict)  # last-24h count at first use

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def sem(self, provider_id: str) -> asyncio.Semaphore:
        return self.registry.semaphore(provider_id)  # shared by every run in this process

    async def emit(self, event_type: str, payload: dict, task_id: str | None = None) -> None:
        await self.store.event(self.run_id, event_type, payload, workspace_id=self.request.workspace_id,
                               project_id=self.request.project_id, task_id=task_id)


@dataclass
class CallOutcome(Generic[T]):
    status: TaskStatus
    value: T | None = None
    provider: str | None = None
    model: str | None = None
    error: str | None = None
    attempts: int = 0
    usage: Usage = field(default_factory=lambda: Usage(estimated_cost_usd=0.0))
    providers_tried: list[str] = field(default_factory=list)


async def call_model(ctx: RunContext, *, actor: str, task_key: str, route: RouteDecision, messages: list[dict],
                     parse: Callable[[str], T], max_tokens: int, timeout_s: float, max_retries: int,
                     max_fallbacks: int, max_cost_usd: float, metadata: dict) -> CallOutcome[T]:
    out: CallOutcome[T] = CallOutcome(status=TaskStatus.failed)
    providers = [route.provider_id] + route.fallbacks[:max_fallbacks]
    est_in = sum(len(m.get("content", "")) for m in messages) // 4
    task_spent = 0.0
    cur_max_tokens = max_tokens  # doubled after a truncated reply

    for pid in providers:
        if not ctx.registry.is_healthy(pid) and pid != route.provider_id:
            continue
        cfg = ctx.registry.configs[pid]
        adapter = ctx.registry.adapters[pid]
        out.providers_tried.append(pid)
        for attempt in range(max_retries + 1):
            if ctx.cancelled:
                out.status, out.error = TaskStatus.cancelled, "run cancelled"
                return out
            blocked = await _quota_block(ctx, pid)
            if blocked:
                await ctx.store.add(PolicyDecisionRow(
                    run_id=ctx.run_id, task_key=task_key, actor=actor, action="model.complete", resource=pid,
                    decision=Decision.DENY.value, rule="provider_quota", reason=blocked))
                out.status, out.error = TaskStatus.failed, f"policy deny: {blocked}"
                break  # try the next provider
            est = cfg.estimate_cost(est_in, cur_max_tokens)
            reserve = est if est is not None else max_cost_usd
            remaining = min(ctx.budget.remaining_usd, max(max_cost_usd - task_spent, 0.0))
            decision = ctx.policy.decide(PolicyInput(
                actor=actor, workspace_id=ctx.request.workspace_id, action="model.complete", resource=pid,
                budget_remaining_usd=remaining, estimated_cost_usd=reserve,
                steps_remaining=ctx.budget.steps_remaining,
            ))
            # Reserve before any await so concurrent tasks cannot all pass the same check.
            if decision.decision is Decision.ALLOW:
                ctx.budget.reserve(reserve)
            await ctx.store.add(PolicyDecisionRow(
                run_id=ctx.run_id, task_key=task_key, actor=actor, action="model.complete", resource=pid,
                decision=decision.decision.value, rule=decision.rule, reason=decision.reason))
            if decision.decision is not Decision.ALLOW:
                out.status = TaskStatus.failed
                out.error = f"policy {decision.decision.value}: {decision.reason}"
                if decision.rule == "budget":
                    await ctx.emit("budget.threshold_reached", {"task": task_key, "reason": decision.reason},
                                   task_id=task_key)
                return out

            out.attempts += 1
            started = time.perf_counter()
            req = CompletionRequest(model=cfg.model, messages=messages, max_tokens=cur_max_tokens,
                                    json_mode=True, metadata=metadata)
            status, error = TaskStatus.failed, None
            retryable = True
            try:
                async with ctx.sem(pid):
                    resp = await asyncio.wait_for(adapter.complete(req), timeout=timeout_s)
            except TimeoutError:
                ctx.budget.settle(reserve, None)  # may have been billed: book conservatively
                ctx.registry.record_failure(pid)
                status, error = TaskStatus.timed_out, f"timeout after {timeout_s}s"
            except ProviderError as e:
                ctx.budget.settle(reserve, 0.0)
                ctx.registry.record_failure(pid)
                error, retryable = f"{e.kind}: {e}", e.retryable
            except asyncio.CancelledError:
                ctx.budget.settle(reserve, None)
                await _record_attempt(ctx, task_key, actor, pid, cfg.model, out.attempts, "cancelled",
                                      "cancelled", started)
                raise
            except Exception as e:  # adapter bug or unexpected I/O failure: a failed attempt, never a lost task
                ctx.budget.settle(reserve, None)
                ctx.registry.record_failure(pid)
                error = f"adapter_error: {type(e).__name__}"  # type only: messages may carry request data
            else:
                booked = ctx.budget.settle(reserve, resp.cost_usd)
                task_spent += booked
                out.usage.input_tokens += resp.input_tokens
                out.usage.output_tokens += resp.output_tokens
                out.usage.estimated_cost_usd = (out.usage.estimated_cost_usd or 0.0) + booked
                await ctx.store.add(UsageRow(
                    run_id=ctx.run_id, task_key=task_key, provider=pid, model=resp.model,
                    input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, cost_usd=booked,
                    cost_is_estimate=int(resp.cost_usd is None)))
                try:
                    value = parse(resp.text)
                except (ValueError, ValidationError) as e:
                    if resp.finish_reason == "length":
                        # Not a provider fault: the cap was too small (reasoning models spend
                        # part of it thinking). Retrying with the same cap would fail again.
                        error = f"truncated_output: reply hit max_tokens={cur_max_tokens}"
                        cur_max_tokens = min(cur_max_tokens * 2, MAX_OUTPUT_TOKENS_CEILING)
                    else:
                        ctx.registry.record_failure(pid)
                        error = f"malformed_output: {str(e).splitlines()[0][:200]}"
                else:
                    ctx.registry.record_success(pid)
                    await _record_attempt(ctx, task_key, actor, pid, cfg.model, out.attempts, "completed",
                                          None, started)
                    out.status, out.value, out.provider, out.model, out.error = (
                        TaskStatus.completed, value, pid, cfg.model, None)
                    return out

            await _record_attempt(ctx, task_key, actor, pid, cfg.model, out.attempts, status.value, error, started)
            out.status, out.error, out.provider, out.model = status, error, pid, cfg.model
            if not retryable:
                break  # go to the next provider
            if attempt < max_retries and ctx.retry_backoff_s > 0:
                await asyncio.sleep(ctx.retry_backoff_s * (2 ** attempt))
    return out


async def _quota_block(ctx: RunContext, pid: str) -> str | None:
    """Enforce max_calls_per_run / max_calls_per_day. Reserves one call when allowed."""
    cfg = ctx.registry.configs[pid]
    if cfg.max_calls_per_run is None and cfg.max_calls_per_day is None:
        return None
    if cfg.max_calls_per_day is not None and pid not in ctx.provider_calls_before_run:
        since = datetime.now(UTC) - timedelta(days=1)
        ctx.provider_calls_before_run[pid] = await ctx.store.count_attempts_since(pid, since)
    # No awaits below: check and reserve atomically with respect to other tasks.
    used = ctx.provider_calls.get(pid, 0)
    if cfg.max_calls_per_run is not None and used >= cfg.max_calls_per_run:
        return f"{pid} reached max_calls_per_run={cfg.max_calls_per_run}"
    if cfg.max_calls_per_day is not None and ctx.provider_calls_before_run[pid] + used >= cfg.max_calls_per_day:
        return f"{pid} reached max_calls_per_day={cfg.max_calls_per_day}"
    ctx.provider_calls[pid] = used + 1
    return None


async def _record_attempt(ctx: RunContext, task_key: str, actor: str, provider: str, model: str, attempt: int,
                          status: str, error: str | None, started: float) -> None:
    await ctx.store.add(AgentRunRow(
        run_id=ctx.run_id, task_key=task_key, attempt=attempt, actor=actor, provider=provider, model=model,
        status=status, error=error, latency_ms=int((time.perf_counter() - started) * 1000)))


async def execute_task(ctx: RunContext, goal: str, task: TaskSpec, upstream: list[AgentResult]) -> AgentResult:
    from museos.contracts import ModelOutput
    from museos.i18n import note
    from museos.prompts import extract_json, task_messages

    route = ctx.router.route_task(task)
    if route is None:
        return AgentResult(task_id=task.task_id, agent_role=task.role.value, status=TaskStatus.failed,
                           error="no healthy provider is available for this task")
    await ctx.store.upsert_task(ctx.run_id, task.task_id, role=task.role.value, spec=task.model_dump(),
                                status=TaskStatus.running.value, provider=route.provider_id, model=route.model)
    await ctx.emit("agent_run.started", {"role": task.role.value, "provider": route.provider_id,
                                         "model": route.model, "reason": route.reason}, task_id=task.task_id)

    outcome = await call_model(
        ctx, actor=task.role.value, task_key=task.task_id, route=route,
        messages=task_messages(goal, task, upstream, ctx.request.language),
        parse=lambda text: ModelOutput.model_validate(extract_json(text)),
        max_tokens=task.max_output_tokens, timeout_s=task.timeout_s, max_retries=task.max_retries,
        max_fallbacks=task.max_fallbacks, max_cost_usd=task.max_cost_usd,
        metadata={"purpose": "task", "role": task.role.value, "idea": goal, "task_id": task.task_id},
    )
    result = AgentResult(task_id=task.task_id, agent_role=task.role.value, provider=outcome.provider,
                         model=outcome.model, status=outcome.status, attempts=outcome.attempts,
                         usage=outcome.usage, error=outcome.error, route_reason=route.reason)
    if outcome.value is not None:
        result = result.model_copy(update=outcome.value.model_dump())
        if outcome.provider != route.provider_id:
            result.limitations.append(note(ctx.request.language, "fallback_provider",
                                           primary=route.provider_id, fallback=outcome.provider))
    if route.unsupported:
        result.limitations.append(note(ctx.request.language, "missing_capabilities",
                                       caps=", ".join(route.unsupported)))

    async def persist() -> None:
        await ctx.store.upsert_task(ctx.run_id, task.task_id, role=task.role.value, spec=task.model_dump(),
                                    status=result.status.value, provider=result.provider, model=result.model,
                                    result=result.model_dump(mode="json"), error=result.error)
        await ctx.emit("agent_run.completed" if result.status is TaskStatus.completed else "agent_run.failed",
                       {"status": result.status.value, "provider": result.provider, "attempts": result.attempts,
                        "error": result.error}, task_id=task.task_id)

    # The model call is over, so the result is final. A cancel that lands while it is being saved must not
    # turn finished work into "cancelled": finish saving and return the result.
    saving = asyncio.ensure_future(persist())
    try:
        await asyncio.shield(saving)
    except asyncio.CancelledError:
        await saving
    return result
