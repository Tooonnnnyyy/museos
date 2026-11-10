# ADR-004: Database and tenant isolation

**Status:** Accepted (Phase 1), isolation deferred · 2026-11-08

**Decision:** PostgreSQL through SQLAlchemy 2 async (`asyncpg`). SQLite (`aiosqlite`) is used only in tests.
Tables: `runs`, `tasks`, `agent_runs`, `usage_records`, `policy_decisions`, `events`. Every row carries
`run_id`, and runs carry `workspace_id`/`project_id`. Runs have a unique `(workspace_id, idempotency_key)`
constraint so duplicate submissions return the same run.

**Deferred:** Alembic migrations (Phase 1 uses `create_all`), API auth, row-level security, and the remaining
entities in the product spec (workspaces, users, connections, sources, artifacts, approvals, memories).
These must land before anyone other than the owner uses the system.
