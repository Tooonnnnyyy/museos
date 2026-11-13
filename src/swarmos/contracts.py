"""Typed contracts shared across SwarmAgentOS."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class RunStatus(StrEnum):
    created = "created"
    clarifying = "clarifying"
    awaiting_input = "awaiting_input"
    planning = "planning"
    queued = "queued"
    running = "running"
    reviewing = "reviewing"
    synthesizing = "synthesizing"
    completed = "completed"
    partial = "partial"
    failed = "failed"
    cancelled = "cancelled"


class TaskStatus(StrEnum):
    blocked = "blocked"
    ready = "ready"
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    timed_out = "timed_out"
    cancelled = "cancelled"
    awaiting_approval = "awaiting_approval"
    skipped = "skipped"


TERMINAL_TASK_STATUSES = {
    TaskStatus.completed,
    TaskStatus.failed,
    TaskStatus.timed_out,
    TaskStatus.cancelled,
    TaskStatus.skipped,
}


class Role(StrEnum):
    product_strategist = "product_strategist"
    research_analyst = "research_analyst"
    technical_architect = "technical_architect"
    ux_designer = "ux_designer"
    business_analyst = "business_analyst"
    critic = "critic"


class CollaborationMode(StrEnum):
    dynamic = "dynamic"  # Chief picks providers per task (default)
    fan_out = "fan_out"  # every task goes to every enabled provider
    manual = "manual"  # user restricts the provider set; Chief routes inside it


Confidence = Literal["low", "medium", "high"]


class TaskSpec(BaseModel):
    """One bounded unit of work in the task graph."""

    task_id: str = Field(pattern=r"^[a-z0-9_\-]{1,64}$")
    role: Role
    objective: str = Field(min_length=1, max_length=2000)
    depends_on: list[str] = Field(default_factory=list)
    dependency_policy: Literal["all", "any"] = "all"
    required_capabilities: list[str] = Field(default_factory=list)
    preferred_providers: list[str] = Field(default_factory=list)
    excluded_providers: list[str] = Field(default_factory=list)
    tools_allowed: list[str] = Field(default_factory=list)
    priority: int = 0
    timeout_s: float = Field(default=90.0, gt=0, le=900)
    max_retries: int = Field(default=1, ge=0, le=5)
    max_fallbacks: int = Field(default=1, ge=0, le=5)
    max_output_tokens: int = Field(default=4000, ge=64, le=16000)
    max_cost_usd: float = Field(default=0.50, ge=0)
    risk_level: Literal["low", "medium", "high"] = "low"


class Plan(BaseModel):
    goal: str
    summary: str = ""
    tasks: list[TaskSpec] = Field(min_length=1)
    requires_cross_review: bool = False
    source: Literal["chief_llm", "fallback_template"] = "chief_llm"
    planner_provider: str | None = None

    @model_validator(mode="after")
    def _validate_dag(self) -> "Plan":
        ids = [t.task_id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate task_id in plan")
        known = set(ids)
        for t in self.tasks:
            missing = [d for d in t.depends_on if d not in known]
            if missing:
                raise ValueError(f"task {t.task_id} depends on unknown tasks {missing}")
            if t.task_id in t.depends_on:
                raise ValueError(f"task {t.task_id} depends on itself")
        # cycle check (Kahn)
        indeg = {t.task_id: len(set(t.depends_on)) for t in self.tasks}
        children: dict[str, list[str]] = {i: [] for i in ids}
        for t in self.tasks:
            for d in set(t.depends_on):
                children[d].append(t.task_id)
        queue = [i for i, n in indeg.items() if n == 0]
        seen = 0
        while queue:
            cur = queue.pop()
            seen += 1
            for c in children[cur]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    queue.append(c)
        if seen != len(ids):
            raise ValueError("task graph contains a cycle")
        return self


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    # None means the provider did not report cost and none could be computed.
    estimated_cost_usd: float | None = None


class Evidence(BaseModel):
    source_id: str
    claim: str
    url: str | None = None
    retrieved_at: str | None = None


class AgentResult(BaseModel):
    task_id: str
    agent_role: str
    provider: str | None = None
    model: str | None = None
    status: TaskStatus
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: Confidence | None = None
    limitations: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    attempts: int = 0
    error: str | None = None
    route_reason: str | None = None


class ModelOutput(BaseModel):
    """The JSON shape a specialist model must return (validated before use)."""

    summary: str
    findings: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: Confidence | None = None
    limitations: list[str] = Field(default_factory=list)

    @field_validator("findings", "recommendations", "assumptions", "risks", "limitations", mode="before")
    @classmethod
    def _coerce_str_list(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return [x if isinstance(x, str) else str(x) for x in v]
        return v


class ReviewOutput(BaseModel):
    verdict: Literal["agree", "partially_agree", "disagree"]
    strengths: list[str] = Field(default_factory=list)
    challenges: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)


class ReviewResult(BaseModel):
    review_id: str
    target_task_id: str
    reviewer_provider: str | None
    reviewer_model: str | None
    status: TaskStatus
    output: ReviewOutput | None = None
    error: str | None = None
    usage: Usage = Field(default_factory=Usage)


class RunRequest(BaseModel):
    idea: str = Field(min_length=3, max_length=20000)
    mode: CollaborationMode = CollaborationMode.dynamic
    providers: list[str] | None = None  # restrict to these providers (manual mode)
    cross_review: bool = False
    budget_usd: float = Field(default=2.0, ge=0)
    max_tasks: int = Field(default=6, ge=1, le=20)
    max_steps: int = Field(default=40, ge=1, le=500)
    concurrency: int = Field(default=4, ge=1, le=32)
    task_timeout_s: float | None = Field(default=None, gt=0, le=900)  # overrides per-task timeout
    language: Literal["en", "fr", "zh"] | None = None  # output language; None = follow the idea
    clarify: bool = False  # Chief asks up to 3 questions before planning
    compare: bool = False  # also ask one single model, for a blind A/B vote
    baseline_provider: str | None = None  # single model for the blind test; None = router's pick
    workspace_id: str = "default"
    project_id: str = "default"


class ClarifyOutput(BaseModel):
    """Chief's reading of the idea before any work is dispatched."""

    # project: worth a team. question: one direct answer settles it. action: asks SwarmAgentOS to do something.
    kind: Literal["project", "question", "action"] = "project"
    answer: str = ""  # filled for question and action; the run then ends without dispatching a team
    goal: str = Field(min_length=1, max_length=2000)
    questions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)

    @field_validator("questions", "assumptions", mode="before")
    @classmethod
    def _cap(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            v = [v]
        return [str(x).strip()[:500] for x in (v or []) if str(x).strip()][:3]


class BriefAnswer(BaseModel):
    """The shape both sides of a blind test are shown in."""

    executive_summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)

    @field_validator("key_points", "open_questions", "next_steps", "risks", "assumptions", mode="before")
    @classmethod
    def _cap(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            v = [v]
        return [str(x).strip() for x in (v or []) if str(x).strip()][:8]


class FailedTask(BaseModel):
    task_id: str
    role: str
    status: TaskStatus
    error: str | None


class DeliverableSection(BaseModel):
    task_id: str
    role: str
    provider: str | None
    model: str | None
    summary: str
    findings: list[str]
    recommendations: list[str]
    confidence: Confidence | None


class Deliverable(BaseModel):
    run_id: str
    goal: str
    status: RunStatus
    executive_summary: str
    synthesis_source: Literal["synthesis_llm", "deterministic", "direct"]
    sections: list[DeliverableSection]
    agreements: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    failed_tasks: list[FailedTask] = Field(default_factory=list)
    reviews: list[ReviewResult] = Field(default_factory=list)
    providers_used: list[str] = Field(default_factory=list)
    usage_total: Usage = Field(default_factory=Usage)
    language: str | None = None
