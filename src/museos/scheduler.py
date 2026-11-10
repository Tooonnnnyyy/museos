"""Fan-out / fan-in DAG scheduler.

Independent tasks run concurrently (bounded by ``concurrency``); dependent tasks
wait for their prerequisites. A failed task never discards completed results;
its dependants are skipped (``dependency_policy="all"``) or run with whatever
upstream output exists (``"any"``). Cancellation stops running tasks and keeps
everything already finished.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from museos.contracts import TERMINAL_TASK_STATUSES, AgentResult, TaskSpec, TaskStatus

RunOne = Callable[[TaskSpec, list[AgentResult]], Awaitable[AgentResult]]
OnStatus = Callable[[TaskSpec, TaskStatus, AgentResult | None], Awaitable[None]]


class DAGScheduler:
    def __init__(self, tasks: list[TaskSpec], run_one: RunOne, *, concurrency: int,
                 cancel_event: asyncio.Event, on_status: OnStatus | None = None):
        self.tasks = {t.task_id: t for t in tasks}
        self.run_one = run_one
        self.concurrency = max(concurrency, 1)
        self.cancel_event = cancel_event
        self.on_status = on_status
        self.status: dict[str, TaskStatus] = {t.task_id: TaskStatus.blocked for t in tasks}
        self.results: dict[str, AgentResult] = {}

    async def _set(self, tid: str, status: TaskStatus, result: AgentResult | None = None) -> None:
        self.status[tid] = status
        if result is not None:
            self.results[tid] = result
        if self.on_status:
            await self.on_status(self.tasks[tid], status, result)

    def _placeholder(self, tid: str, status: TaskStatus, error: str) -> AgentResult:
        return AgentResult(task_id=tid, agent_role=self.tasks[tid].role.value, status=status, error=error)

    async def _resolve_blocked(self) -> None:
        changed = True
        while changed:
            changed = False
            for tid, t in self.tasks.items():
                if self.status[tid] is not TaskStatus.blocked:
                    continue
                deps = [self.status[d] for d in t.depends_on]
                if any(s not in TERMINAL_TASK_STATUSES for s in deps):
                    continue
                done = sum(s is TaskStatus.completed for s in deps)
                ok = done == len(deps) if t.dependency_policy == "all" else (done > 0 or not deps)
                if ok:
                    await self._set(tid, TaskStatus.ready)
                else:
                    failed = [d for d in t.depends_on if self.status[d] is not TaskStatus.completed]
                    await self._set(tid, TaskStatus.skipped,
                                    self._placeholder(tid, TaskStatus.skipped,
                                                      f"skipped: upstream {failed} did not complete"))
                changed = True

    async def run(self) -> dict[str, AgentResult]:
        running: dict[asyncio.Task, str] = {}
        cancel_waiter = asyncio.create_task(self.cancel_event.wait())
        try:
            while True:
                await self._resolve_blocked()
                if not self.cancel_event.is_set():
                    ready = sorted((t for tid, t in self.tasks.items() if self.status[tid] is TaskStatus.ready),
                                   key=lambda t: -t.priority)
                    for t in ready:
                        if len(running) >= self.concurrency:
                            break
                        upstream = [self.results[d] for d in t.depends_on
                                    if self.status[d] is TaskStatus.completed]
                        await self._set(t.task_id, TaskStatus.running)
                        running[asyncio.create_task(self.run_one(t, upstream))] = t.task_id
                if not running:
                    break
                done, _ = await asyncio.wait([*running, cancel_waiter], return_when=asyncio.FIRST_COMPLETED)
                for fut in done:
                    if fut is cancel_waiter:
                        continue
                    tid = running.pop(fut)
                    await self._finish(tid, fut)
                if self.cancel_event.is_set():
                    for fut in running:
                        fut.cancel()
                    await asyncio.gather(*running, return_exceptions=True)
                    for fut, tid in running.items():
                        await self._finish(tid, fut)
                    running.clear()
                    break
        finally:
            cancel_waiter.cancel()
            for fut in running:
                fut.cancel()

        for tid, s in self.status.items():
            if s not in TERMINAL_TASK_STATUSES:
                await self._set(tid, TaskStatus.cancelled,
                                self._placeholder(tid, TaskStatus.cancelled, "run cancelled before task finished"))
        return self.results

    async def _finish(self, tid: str, fut: asyncio.Task) -> None:
        if fut.cancelled():
            res = self._placeholder(tid, TaskStatus.cancelled, "cancelled while running")
        elif fut.exception() is not None:
            exc = fut.exception()
            res = self._placeholder(tid, TaskStatus.failed, f"internal error: {type(exc).__name__}: {exc}")
        else:
            res = fut.result()
        await self._set(tid, res.status, res)
