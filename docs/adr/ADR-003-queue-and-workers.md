# ADR-003: Queue and worker implementation

**Status:** Deferred · 2026-11-08

**Decision for Phase 1:** No Redis yet. Runs execute as asyncio tasks in the API (or CLI) process.
PostgreSQL is the durable record of runs, tasks, attempts, usage, policy decisions, and events.

**Why:** One process with asyncio handles the concurrency the MVP needs (I/O-bound model calls). Adding a
queue now would add infrastructure without being able to test it against real load.

**Revisit when:** runs must survive process restarts, more than one worker is needed, or inbound webhooks
(Phase 5) must be acknowledged quickly and processed later. Candidates: Redis with arq/RQ/Dramatiq, or a
durable workflow engine. Redis Pub/Sub alone is not durable task storage.
