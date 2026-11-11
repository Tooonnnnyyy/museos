"""Durable persistence (PostgreSQL in dev/prod; SQLite only for tests).

Tables: runs, tasks,
agent_runs, usage_records, policy_decisions, events. Schema is created with
``create_all``; Alembic migrations are a tracked follow-up (see ADR-004).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy import delete as sa_delete
from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idea: Mapped[str] = mapped_column(Text)
    request: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), index=True)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    deliverable: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    spent_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key", name="uq_runs_idempotency"),)


class TaskRow(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    task_key: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16), default="task")  # task | review
    role: Mapped[str] = mapped_column(String(64))
    spec: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    __table_args__ = (UniqueConstraint("run_id", "task_key", name="uq_tasks_run_key"),)


class AgentRunRow(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    task_key: Mapped[str] = mapped_column(String(128))
    attempt: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UsageRow(Base):
    __tablename__ = "usage_records"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    task_key: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cost_is_estimate: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PolicyDecisionRow(Base):
    __tablename__ = "policy_decisions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    task_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(128))
    resource: Mapped[str | None] = mapped_column(String(256), nullable=True)
    decision: Mapped[str] = mapped_column(String(32))
    rule: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EventRow(Base):
    __tablename__ = "events"
    event_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    workspace_id: Mapped[str] = mapped_column(String(64))
    project_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(32))
    causation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    payload_version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON)


class ComparisonRow(Base):
    """One blind A/B test: a MuseOS run against one single-model answer to the same brief."""

    __tablename__ = "comparisons"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), unique=True, index=True)
    museos_slot: Mapped[str] = mapped_column(String(1))  # "A" or "B": which side shows the MuseOS result
    baseline_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|running|completed|failed
    baseline: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    baseline_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    baseline_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    baseline_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    baseline_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    baseline_latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    choice: Mapped[str | None] = mapped_column(String(8), nullable=True)  # A | B | tie
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    voted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Store:
    def __init__(self, database_url: str):
        self.engine: AsyncEngine = create_async_engine(database_url, future=True)
        if database_url.startswith("sqlite"):
            # Parallel agents write at the same time: WAL lets readers and a writer overlap, and the
            # busy timeout makes a second writer wait instead of failing with "database is locked".
            @sa_event.listens_for(self.engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record) -> None:
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=15000")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self._seq: dict[str, int] = {}

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    # ------------------------------------------------------------------ runs
    async def create_run(self, *, idea: str, request: dict[str, Any], workspace_id: str, project_id: str,
                         idempotency_key: str | None) -> tuple[RunRow, bool]:
        """Create a run, or return the existing one for a repeated idempotency key."""
        async with self.sessions() as s:
            if idempotency_key:
                existing = (await s.execute(select(RunRow).where(
                    RunRow.workspace_id == workspace_id, RunRow.idempotency_key == idempotency_key))).scalar_one_or_none()
                if existing:
                    return existing, False
            row = RunRow(idea=idea, request=request, status="created", workspace_id=workspace_id,
                         project_id=project_id, idempotency_key=idempotency_key)
            s.add(row)
            try:
                await s.commit()
            except Exception:
                await s.rollback()
                if idempotency_key:  # lost a race with a concurrent duplicate
                    existing = (await s.execute(select(RunRow).where(
                        RunRow.workspace_id == workspace_id,
                        RunRow.idempotency_key == idempotency_key))).scalar_one()
                    return existing, False
                raise
            return row, True

    async def update_run(self, run_id: str, **fields: Any) -> None:
        async with self.sessions() as s:
            row = await s.get(RunRow, run_id)
            if row is None:
                return
            for k, v in fields.items():
                setattr(row, k, v)
            await s.commit()

    async def claim_run(self, run_id: str, from_status: str, to_status: str) -> bool:
        """Move a run from one status to another in a single statement. True for exactly one caller,
        so two requests arriving together cannot both act on the same run."""
        async with self.sessions() as s:
            res = await s.execute(update(RunRow).where(RunRow.id == run_id, RunRow.status == from_status)
                                  .values(status=to_status))
            await s.commit()
            return res.rowcount == 1

    async def get_run(self, run_id: str) -> RunRow | None:
        async with self.sessions() as s:
            return await s.get(RunRow, run_id)

    async def list_runs(self, limit: int = 20) -> list[RunRow]:
        async with self.sessions() as s:
            return list((await s.execute(select(RunRow).order_by(RunRow.created_at.desc()).limit(limit))).scalars())

    async def get_comparison(self, comparison_id: str) -> ComparisonRow | None:
        async with self.sessions() as s:
            return await s.get(ComparisonRow, comparison_id)

    async def get_comparison_by_run(self, run_id: str) -> ComparisonRow | None:
        async with self.sessions() as s:
            return (await s.execute(select(ComparisonRow).where(ComparisonRow.run_id == run_id))).scalar_one_or_none()

    async def list_comparisons(self, limit: int = 100) -> list[ComparisonRow]:
        async with self.sessions() as s:
            return list((await s.execute(select(ComparisonRow).order_by(ComparisonRow.created_at.desc())
                                         .limit(limit))).scalars())

    async def update_comparison(self, comparison_id: str, **fields: Any) -> None:
        async with self.sessions() as s:
            row = await s.get(ComparisonRow, comparison_id)
            if row is None:
                return
            for k, v in fields.items():
                setattr(row, k, v)
            await s.commit()

    async def delete_comparison(self, comparison_id: str) -> None:
        async with self.sessions() as s:
            await s.execute(sa_delete(ComparisonRow).where(ComparisonRow.id == comparison_id))
            await s.commit()

    async def recent(self, model: type[Base], limit: int = 5000) -> list[Any]:
        """Newest rows of one table, for the admin summary. Local, single-user data stays small."""
        async with self.sessions() as s:
            return list((await s.execute(select(model).order_by(model.created_at.desc()).limit(limit))).scalars())

    async def find_event(self, run_id: str, event_type: str) -> EventRow | None:
        async with self.sessions() as s:
            return (await s.execute(select(EventRow).where(EventRow.run_id == run_id, EventRow.event_type == event_type)
                                    .order_by(EventRow.occurred_at.desc()).limit(1))).scalars().first()

    async def list_tasks(self, run_id: str) -> list[TaskRow]:
        async with self.sessions() as s:
            return list((await s.execute(select(TaskRow).where(TaskRow.run_id == run_id)
                                         .order_by(TaskRow.kind, TaskRow.task_key))).scalars())

    async def list_events(self, run_id: str) -> list[EventRow]:
        async with self.sessions() as s:
            return list((await s.execute(select(EventRow).where(EventRow.run_id == run_id)
                                         .order_by(EventRow.seq))).scalars())

    async def list_usage(self, run_id: str) -> list[UsageRow]:
        async with self.sessions() as s:
            return list((await s.execute(select(UsageRow).where(UsageRow.run_id == run_id))).scalars())

    async def list_agent_runs(self, run_id: str) -> list[AgentRunRow]:
        async with self.sessions() as s:
            return list((await s.execute(select(AgentRunRow).where(AgentRunRow.run_id == run_id)
                                         .order_by(AgentRunRow.created_at))).scalars())

    async def count_attempts_since(self, provider: str, since: datetime) -> int:
        from sqlalchemy import func

        async with self.sessions() as s:
            q = select(func.count()).select_from(AgentRunRow).where(
                AgentRunRow.provider == provider, AgentRunRow.created_at >= since)
            return int((await s.execute(q)).scalar_one())

    async def list_policy_decisions(self, run_id: str) -> list[PolicyDecisionRow]:
        async with self.sessions() as s:
            return list((await s.execute(select(PolicyDecisionRow)
                                         .where(PolicyDecisionRow.run_id == run_id))).scalars())

    # ------------------------------------------------------------------ records
    async def upsert_task(self, run_id: str, task_key: str, *, kind: str = "task", role: str,
                          spec: dict[str, Any], status: str, provider: str | None = None, model: str | None = None,
                          result: dict[str, Any] | None = None, error: str | None = None) -> None:
        async with self.sessions() as s:
            row = (await s.execute(select(TaskRow).where(TaskRow.run_id == run_id,
                                                         TaskRow.task_key == task_key))).scalar_one_or_none()
            if row is None:
                row = TaskRow(run_id=run_id, task_key=task_key, kind=kind, role=role, spec=spec, status=status)
                s.add(row)
            row.status = status
            if provider is not None:
                row.provider = provider
            if model is not None:
                row.model = model
            if result is not None:
                row.result = result
            if error is not None:
                row.error = error
            await s.commit()

    async def add(self, row: Base) -> None:
        async with self.sessions() as s:
            s.add(row)
            await s.commit()

    async def event(self, run_id: str, event_type: str, payload: dict[str, Any], *, workspace_id: str,
                    project_id: str, task_id: str | None = None, causation_id: str | None = None) -> str:
        seq = self._seq.get(run_id, 0) + 1
        self._seq[run_id] = seq
        row = EventRow(seq=seq, event_type=event_type, workspace_id=workspace_id, project_id=project_id,
                       run_id=run_id, task_id=task_id, correlation_id=run_id, causation_id=causation_id,
                       payload=payload)
        await self.add(row)
        return row.event_id
