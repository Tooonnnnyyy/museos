# ADR-002: Agent orchestration and checkpointing

**Status:** Accepted (Phase 1) · 2026-11-08

**Decision:** LangGraph runs the run-level state machine `plan → execute → [review] → synthesize`, with a
checkpointer keyed by `run_id`. Inside `execute`, a purpose-built `DAGScheduler` runs the Chief's dynamic
task graph (asyncio fan-out/fan-in, dependency policies, concurrency limit, cancellation).

**Why:** The Chief decides the number and shape of tasks at runtime. A plain asyncio DAG runner covers
dependencies, partial failure and cancellation in a way that is easy to test. LangGraph still owns the
coarse states, where checkpoint/resume and conditional routing matter. The review round is bounded to one
by construction: there is no edge from `review` back to `execute`.

**Consequences:**
- Phase 1 uses `InMemorySaver`, so a process restart loses in-flight run state. Next step:
  `langgraph-checkpoint-postgres` and resuming interrupted runs on startup.
- If tasks later need per-task human approval (`awaiting_approval`), consider moving task nodes into
  LangGraph with `Send` and interrupts.
