"""Chief planner: goal -> validated, typed task plan.

The plan comes from an LLM when one is reachable and returns a valid DAG.
Otherwise SwarmAgentOS falls back to a fixed template and says so in the deliverable.
"""

from __future__ import annotations

import re
from typing import Any

from swarmos.contracts import CollaborationMode, Plan, RunRequest, TaskSpec
from swarmos.engine import RunContext, call_model
from swarmos.i18n import note
from swarmos.prompts import extract_json, plan_messages


def fallback_plan(goal: str, max_tasks: int) -> Plan:
    tasks = [
        TaskSpec(task_id="product", role="product_strategist",
                 objective="Define the problem, target users, value proposition and MVP scope."),
        TaskSpec(task_id="research", role="research_analyst",
                 objective="Identify market context, competitors and user pain points; list what needs verifying.",
                 required_capabilities=["research"]),
        TaskSpec(task_id="technical", role="technical_architect",
                 objective="Propose a system architecture and assess technical feasibility.",
                 required_capabilities=["code"]),
        TaskSpec(task_id="critic", role="critic",
                 objective="Challenge the product and technical proposals; list failure modes and evidence gaps.",
                 depends_on=["product", "technical"], dependency_policy="any"),
    ][:max_tasks]
    keep = {t.task_id for t in tasks}
    for t in tasks:
        t.depends_on = [d for d in t.depends_on if d in keep]
    return Plan(goal=goal, summary="Fallback template plan (Chief LLM plan unavailable).", tasks=tasks,
                source="fallback_template")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_\-]", "_", s.lower())[:64] or "task"


def parse_plan(text: str, goal: str, max_tasks: int) -> Plan:
    obj: dict[str, Any] = extract_json(text)
    raw_tasks = obj.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("plan has no tasks")
    raw_tasks = raw_tasks[:max_tasks]
    ids = {_slug(str(t.get("task_id", ""))) for t in raw_tasks if isinstance(t, dict)}
    tasks = []
    for t in raw_tasks:
        if not isinstance(t, dict):
            raise ValueError("task entry is not an object")
        tasks.append({
            "task_id": _slug(str(t.get("task_id", ""))),
            "role": t.get("role"),
            "objective": t.get("objective", ""),
            "depends_on": [_slug(str(d)) for d in t.get("depends_on", []) if _slug(str(d)) in ids],
            "dependency_policy": t.get("dependency_policy", "all"),
            "required_capabilities": [str(c) for c in t.get("required_capabilities", [])][:6],
        })
    return Plan.model_validate({"goal": goal, "summary": str(obj.get("summary", ""))[:2000], "tasks": tasks})


def expand_fan_out(plan: Plan, providers: list[str]) -> Plan:
    """All-model fan-out: one copy of every task per provider, pinned to that provider."""
    def key(tid: str, p: str) -> str:
        return _slug(f"{tid}__{p}")

    tasks = []
    for t in plan.tasks:
        for p in providers:
            tasks.append(t.model_copy(update={
                "task_id": key(t.task_id, p),
                "preferred_providers": [p],
                "excluded_providers": [x for x in providers if x != p],
                "max_fallbacks": 0,
                "depends_on": [key(d, q) for d in t.depends_on for q in providers],
                "dependency_policy": "any" if t.depends_on else t.dependency_policy,
            }))
    return plan.model_copy(update={"tasks": tasks})


async def make_plan(ctx: RunContext, request: RunRequest) -> Plan:
    plan: Plan | None = None
    route = ctx.router.route(["planning"], count_load=False)
    if route is not None:
        outcome = await call_model(
            ctx, actor="chief", task_key="__plan__", route=route,
            messages=plan_messages(request.idea, request.max_tasks, request.language),
            parse=lambda text: parse_plan(text, request.idea, request.max_tasks),
            max_tokens=4000, timeout_s=120, max_retries=1, max_fallbacks=1,
            max_cost_usd=min(0.5, request.budget_usd),
            metadata={"purpose": "plan", "idea": request.idea, "max_tasks": request.max_tasks},
        )
        if outcome.value is not None:
            plan = outcome.value.model_copy(update={"planner_provider": outcome.provider})
        else:
            ctx.notes.append(note(request.language, "plan_call_failed", error=outcome.error))
            await ctx.emit("run.plan.fallback", {"error": outcome.error})
    else:
        ctx.notes.append(note(request.language, "no_planner"))
    if plan is None:
        plan = fallback_plan(request.idea, request.max_tasks)

    plan = plan.model_copy(update={"requires_cross_review": request.cross_review})
    if request.task_timeout_s:
        plan.tasks = [t.model_copy(update={"timeout_s": request.task_timeout_s}) for t in plan.tasks]
    if request.mode is CollaborationMode.fan_out:
        plan = expand_fan_out(plan, ctx.router.allowed)
    return plan
