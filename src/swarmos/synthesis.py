"""Bounded cross-review and final synthesis."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from swarmos.contracts import (
    AgentResult,
    Deliverable,
    DeliverableSection,
    FailedTask,
    Plan,
    ReviewOutput,
    ReviewResult,
    Role,
    RunStatus,
    TaskStatus,
    Usage,
)
from swarmos.engine import RunContext, call_model
from swarmos.i18n import heading, note
from swarmos.prompts import extract_json, review_messages, synthesis_messages


class SynthesisOutput(BaseModel):
    executive_summary: str
    agreements: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


async def cross_review(ctx: RunContext, goal: str, results: list[AgentResult]) -> list[ReviewResult]:
    """One bounded round: each completed non-critic result is reviewed once by a different provider."""
    targets = [r for r in results if r.status is TaskStatus.completed and r.agent_role != Role.critic.value]
    sem = asyncio.Semaphore(ctx.request.concurrency)

    async def review_one(target: AgentResult) -> ReviewResult:
        rid = f"review__{target.task_id}"
        exclude = (target.provider,) if target.provider else ()
        route = ctx.router.route(["review"], exclude_also=exclude) or ctx.router.route(["review"])
        if route is None:
            return ReviewResult(review_id=rid, target_task_id=target.task_id, reviewer_provider=None,
                                reviewer_model=None, status=TaskStatus.failed, error="no healthy reviewer")
        same = route.provider_id == target.provider
        await ctx.store.upsert_task(ctx.run_id, rid, kind="review", role="cross_reviewer",
                                    spec={"target": target.task_id}, status="running",
                                    provider=route.provider_id, model=route.model)
        async with sem:
            outcome = await call_model(
                ctx, actor="cross_reviewer", task_key=rid, route=route,
                messages=review_messages(goal, target, ctx.request.language),
                parse=lambda text: ReviewOutput.model_validate(extract_json(text)),
                max_tokens=2000, timeout_s=90, max_retries=1, max_fallbacks=1, max_cost_usd=0.25,
                metadata={"purpose": "review", "idea": goal, "target": target.task_id},
            )
        rr = ReviewResult(review_id=rid, target_task_id=target.task_id, reviewer_provider=outcome.provider,
                          reviewer_model=outcome.model, status=outcome.status, output=outcome.value,
                          error=outcome.error, usage=outcome.usage)
        if same and outcome.value is not None:
            ctx.notes.append(note(ctx.request.language, "same_reviewer", task=target.task_id))
        await ctx.store.upsert_task(ctx.run_id, rid, kind="review", role="cross_reviewer",
                                    spec={"target": target.task_id}, status=rr.status.value,
                                    provider=rr.reviewer_provider, model=rr.reviewer_model,
                                    result=rr.model_dump(mode="json"), error=rr.error)
        return rr

    await ctx.emit("review.started", {"targets": [t.task_id for t in targets], "rounds": 1})
    reviews = list(await asyncio.gather(*(review_one(t) for t in targets)))
    await ctx.emit("review.completed", {"completed": sum(r.status is TaskStatus.completed for r in reviews)})
    return reviews


def _dedupe(items: list[str], limit: int = 30) -> list[str]:
    seen, out = set(), []
    for i in items:
        k = i.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(i.strip())
    return out[:limit]


def run_status(results: list[AgentResult], cancelled: bool) -> RunStatus:
    done = sum(r.status is TaskStatus.completed for r in results)
    if cancelled:
        return RunStatus.cancelled
    if done == len(results) and results:
        return RunStatus.completed
    return RunStatus.partial if done else RunStatus.failed


async def synthesize(ctx: RunContext, plan: Plan, results: list[AgentResult],
                     reviews: list[ReviewResult]) -> Deliverable:
    completed = [r for r in results if r.status is TaskStatus.completed]
    not_done = [r for r in results if r.status is not TaskStatus.completed]
    status = run_status(results, ctx.cancelled)

    lang = ctx.request.language
    limitations: list[str] = []
    if not_done:
        limitations.append(note(lang, "partial", n=len(not_done), total=len(results),
                                detail=", ".join(f"{r.task_id}={r.status.value}" for r in not_done)))
    if plan.source == "fallback_template":
        limitations.append(note(lang, "template_plan"))
    if ctx.budget.unknown_cost_calls:
        limitations.append(note(lang, "unknown_cost", n=ctx.budget.unknown_cost_calls))
    used = sorted({r.provider for r in completed if r.provider})
    if any(ctx.registry.configs[p].adapter == "mock" for p in used):
        limitations.append(note(lang, "mock_used"))
    limitations += ctx.notes
    for r in completed:
        limitations += r.limitations

    exec_summary, agreements, disagreements, next_steps = "", [], [], []
    source = "deterministic"
    route = ctx.router.route(["synthesis"], count_load=False) if completed and not ctx.cancelled else None
    if route is not None:
        reviews_digest: list[dict[str, Any]] = [
            {"target": rv.target_task_id, "reviewer": rv.reviewer_provider, **rv.output.model_dump()}
            for rv in reviews if rv.output is not None]
        outcome = await call_model(
            ctx, actor="synthesis_editor", task_key="__synthesis__", route=route,
            messages=synthesis_messages(plan.goal, completed, reviews_digest, [r.task_id for r in not_done], lang),
            parse=lambda text: SynthesisOutput.model_validate(extract_json(text)),
            max_tokens=4000, timeout_s=150, max_retries=1, max_fallbacks=1,
            max_cost_usd=min(0.5, max(ctx.budget.remaining_usd, 0.0)),
            metadata={"purpose": "synthesis", "idea": plan.goal},
        )
        if outcome.value is not None:
            source = "synthesis_llm"
            v = outcome.value
            exec_summary, agreements, disagreements, next_steps = (
                v.executive_summary, v.agreements, v.disagreements, v.next_steps)
        else:
            limitations.append(note(lang, "synthesis_failed", error=outcome.error))
    if not exec_summary:
        exec_summary = (note(lang, "fallback_summary", done=len(completed), total=len(results), goal=plan.goal[:200])
                        + " " + " ".join(r.summary for r in completed[:3]))[:2000]
    for rv in reviews:
        if rv.output is not None and rv.output.verdict != "agree":
            # Language-neutral prefix; blindtest strips these lines by this shape.
            disagreements += [f"[{rv.reviewer_provider} → {rv.target_task_id}] {c}"
                              for c in rv.output.challenges]

    total = Usage(estimated_cost_usd=round(ctx.budget.spent_usd, 6))
    for r in results:
        total.input_tokens += r.usage.input_tokens
        total.output_tokens += r.usage.output_tokens
    for rv in reviews:
        total.input_tokens += rv.usage.input_tokens
        total.output_tokens += rv.usage.output_tokens

    return Deliverable(
        run_id=ctx.run_id, goal=plan.goal, status=status, executive_summary=exec_summary, language=lang,
        synthesis_source=source,
        sections=[DeliverableSection(task_id=r.task_id, role=r.agent_role, provider=r.provider, model=r.model,
                                     summary=r.summary, findings=r.findings, recommendations=r.recommendations,
                                     confidence=r.confidence) for r in completed],
        agreements=_dedupe(agreements), disagreements=_dedupe(disagreements), next_steps=_dedupe(next_steps),
        assumptions=_dedupe([a for r in completed for a in r.assumptions]),
        risks=_dedupe([x for r in completed for x in r.risks]),
        evidence=[e for r in completed for e in r.evidence],
        limitations=_dedupe(limitations, limit=50),
        failed_tasks=[FailedTask(task_id=r.task_id, role=r.agent_role, status=r.status, error=r.error)
                      for r in not_done],
        reviews=reviews, providers_used=used, usage_total=total,
    )


def render_markdown(d: Deliverable) -> str:
    lang = d.language

    def h(key: str) -> str:
        return heading(lang, key)

    lines = [f"# {h('title')} `{d.run_id}`", "", f"**{h('goal')}:** {d.goal}", "",
             f"**{h('status')}:** {d.status.value} · **{h('providers')}:** {', '.join(d.providers_used) or '—'} · "
             f"**{h('cost')}:** ${d.usage_total.estimated_cost_usd or 0:.4f} · "
             f"**{h('tokens')}:** {d.usage_total.input_tokens} in / {d.usage_total.output_tokens} out", "",
             f"## {h('summary')}", "", d.executive_summary, ""]

    def block(key: str, items: list[str]) -> None:
        if items:
            lines.extend([f"## {h(key)}", "", *[f"- {i}" for i in items], ""])

    block("agreements", d.agreements)
    block("disagreements", d.disagreements)
    block("next_steps", d.next_steps)
    for s in d.sections:
        lines.extend([f"## {s.role} (`{s.task_id}`) — {s.provider}/{s.model}", "", s.summary, ""])
        lines.extend(f"- {f}" for f in s.findings)
        if s.recommendations:
            lines.extend(["", f"**{h('recommendations')}**", *[f"- {r}" for r in s.recommendations]])
        lines.append("")
    block("assumptions", d.assumptions)
    block("risks", d.risks)
    if d.evidence:
        block("evidence", [f"{e.claim} — {e.url or e.source_id}" for e in d.evidence])
    else:
        block("evidence", [note(lang, "no_evidence")])
    block("failed", [f"{f.task_id} ({f.role}): {f.status.value} — {f.error}" for f in d.failed_tasks])
    block("limitations", d.limitations)
    return "\n".join(lines)
