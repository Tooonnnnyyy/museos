"""Deterministic mock provider for local runs and tests.

Produces schema-valid JSON for each request purpose so the whole pipeline can be
exercised without API keys. Output is clearly labelled as mock content and never
contains fabricated evidence or URLs.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, ClassVar

from swarmos.gateway.base import CompletionRequest, CompletionResponse, ProviderConfig, ProviderError


@dataclass
class MockBehavior:
    latency_s: float = 0.05
    fail_first_n: int = 0  # fail this many calls, then succeed
    always_fail: bool = False
    fail_retryable: bool = True
    malformed: bool = False  # return non-JSON text
    hang: bool = False  # never return (exercise timeouts)
    direct_kind: str = ""  # clarify step classifies the request as 'question' or 'action'
    no_questions: bool = False  # clarify step returns no questions
    truncate_below: int = 0  # if max_tokens < this, return cut-off JSON with finish_reason=length


class MockProvider:
    # Global in-flight counter across all mock providers, for concurrency tests.
    inflight: ClassVar[int] = 0
    max_inflight: ClassVar[int] = 0

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.provider_id = config.id
        self.behavior = MockBehavior(**config.mock)
        self.calls = 0

    @classmethod
    def reset_counters(cls) -> None:
        cls.inflight = 0
        cls.max_inflight = 0

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        call_no = self.calls
        started = time.perf_counter()
        MockProvider.inflight += 1
        MockProvider.max_inflight = max(MockProvider.max_inflight, MockProvider.inflight)
        try:
            if self.behavior.hang:
                await asyncio.sleep(3600)
            await asyncio.sleep(self.behavior.latency_s)
            if self.behavior.always_fail or call_no <= self.behavior.fail_first_n:
                raise ProviderError(
                    f"mock provider {self.provider_id} simulated failure (call {call_no})",
                    retryable=self.behavior.fail_retryable,
                    kind="simulated_failure",
                )
            finish = "stop"
            if self.behavior.malformed:
                text = "Sure! Here is my analysis (not JSON) ..."
            else:
                text = json.dumps(self._payload(request), ensure_ascii=False)
                if request.max_tokens < self.behavior.truncate_below:
                    text, finish = text[: len(text) // 2], "length"
        finally:
            MockProvider.inflight -= 1

        in_tok = sum(len(m.get("content", "")) for m in request.messages) // 4
        out_tok = len(text) // 4
        return CompletionResponse(
            text=text,
            provider=self.provider_id,
            model=request.model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=self.config.estimate_cost(in_tok, out_tok),
            finish_reason=finish,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # ------------------------------------------------------------------ payloads
    def _payload(self, req: CompletionRequest) -> dict[str, Any]:
        md = req.metadata
        purpose = md.get("purpose")
        idea = str(md.get("idea", ""))[:80]
        tag = f"[mock:{self.provider_id}]"
        if purpose == "plan":
            return self._plan(idea, int(md.get("max_tasks", 6)))
        if purpose == "clarify":
            if self.behavior.direct_kind:
                return {"kind": self.behavior.direct_kind, "answer": f"{tag} Direct answer to: {idea}",
                        "goal": idea or "request", "questions": [], "assumptions": []}
            return {
                "kind": "project", "answer": "",
                "goal": f"{tag} Validate and scope: {idea}",
                "questions": [] if self.behavior.no_questions else [f"{tag} Who is the first user?",
                                                                     f"{tag} What does success look like in 4 weeks?"],
                "assumptions": [f"{tag} Solo builder, small budget."],
            }
        if purpose == "baseline":
            return {
                "executive_summary": f"{tag} Single-model answer for: {idea}",
                "key_points": [f"{tag} Start with the core user value."],
                "open_questions": [f"{tag} Is demand proven?"],
                "next_steps": [f"{tag} Talk to five users."],
                "risks": [f"{tag} Unvalidated demand."],
                "assumptions": [f"{tag} Mock output."],
            }
        if purpose == "review":
            return {
                "verdict": "partially_agree",
                "strengths": [f"{tag} The analysis addresses the stated objective."],
                "challenges": [f"{tag} Key assumptions are not yet validated with real users."],
                "missing_evidence": [f"{tag} No external sources were retrieved in this run."],
            }
        if purpose == "synthesis":
            return {
                "executive_summary": f"{tag} Consolidated view of '{idea}' from the specialist outputs. "
                "This is mock content produced without a real model.",
                "agreements": [f"{tag} Specialists agree an MVP should validate the core user value first."],
                "disagreements": [f"{tag} Reviewers question whether demand is proven."],
                "next_steps": [f"{tag} Connect at least two real providers and rerun."],
            }
        role = md.get("role", "specialist")
        return {
            "summary": f"{tag} {role} analysis of: {idea}",
            "findings": [f"{tag} {role} finding 1 for the idea.", f"{tag} {role} finding 2 for the idea."],
            "recommendations": [f"{tag} {role} recommends a narrowly scoped MVP."],
            "assumptions": [f"{tag} Mock output: no real reasoning or research was performed."],
            "risks": [f"{tag} {role} risk: unvalidated demand."],
            "evidence": [],
            "confidence": "low",
            "limitations": ["Generated by a mock provider."],
        }

    @staticmethod
    def _plan(idea: str, max_tasks: int) -> dict[str, Any]:
        tasks = [
            {"task_id": "product", "role": "product_strategist",
             "objective": f"Define problem, users, value proposition and MVP scope for: {idea}"},
            {"task_id": "research", "role": "research_analyst",
             "objective": f"Identify market, competitors and user pain points for: {idea}",
             "required_capabilities": ["research"]},
            {"task_id": "technical", "role": "technical_architect",
             "objective": f"Propose system architecture and feasibility for: {idea}",
             "required_capabilities": ["code"]},
            {"task_id": "business", "role": "business_analyst",
             "objective": "Assess business model and cost drivers given the product definition.",
             "depends_on": ["product"]},
            {"task_id": "critic", "role": "critic",
             "objective": "Challenge assumptions in the product and technical proposals.",
             "depends_on": ["product", "technical"], "dependency_policy": "any"},
        ]
        tasks = tasks[:max_tasks]
        keep = {t["task_id"] for t in tasks}
        for t in tasks:
            t["depends_on"] = [d for d in t.get("depends_on", []) if d in keep]
        return {
            "summary": f"Decompose '{idea}' into product, research, technical, business and critique workstreams.",
            "tasks": tasks,
            "requires_cross_review": False,
        }
