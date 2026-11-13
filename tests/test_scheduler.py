"""DAG scheduler: concurrency, dependencies, partial failure, cancellation."""

from __future__ import annotations

import asyncio
import time

from swarmos.contracts import AgentResult, TaskSpec, TaskStatus
from swarmos.scheduler import DAGScheduler


def spec(tid: str, deps: list[str] | None = None, policy: str = "all") -> TaskSpec:
    return TaskSpec(task_id=tid, role="critic", objective="x", depends_on=deps or [], dependency_policy=policy)


def runner(log: list, delay: float = 0.2, fail: set[str] = frozenset(), boom: set[str] = frozenset()):
    async def run_one(t: TaskSpec, upstream: list[AgentResult]) -> AgentResult:
        log.append(("start", t.task_id, time.perf_counter(), [u.task_id for u in upstream]))
        await asyncio.sleep(delay)
        log.append(("end", t.task_id, time.perf_counter()))
        if t.task_id in boom:
            raise RuntimeError("kaboom")
        status = TaskStatus.failed if t.task_id in fail else TaskStatus.completed
        return AgentResult(task_id=t.task_id, agent_role="critic", status=status)
    return run_one


async def test_independent_tasks_run_concurrently():
    log: list = []
    tasks = [spec("a"), spec("b"), spec("c")]
    started = time.perf_counter()
    res = await DAGScheduler(tasks, runner(log, 0.3), concurrency=4, cancel_event=asyncio.Event()).run()
    elapsed = time.perf_counter() - started
    assert all(r.status is TaskStatus.completed for r in res.values())
    assert elapsed < 0.55, f"tasks ran sequentially ({elapsed:.2f}s)"


async def test_concurrency_limit_is_respected():
    log: list = []
    tasks = [spec(f"t{i}") for i in range(4)]
    started = time.perf_counter()
    await DAGScheduler(tasks, runner(log, 0.2), concurrency=2, cancel_event=asyncio.Event()).run()
    assert time.perf_counter() - started >= 0.39


async def test_two_tasks_depend_on_one_upstream():
    log: list = []
    tasks = [spec("up"), spec("x", ["up"]), spec("y", ["up"])]
    res = await DAGScheduler(tasks, runner(log, 0.1), concurrency=4, cancel_event=asyncio.Event()).run()
    up_end = next(e[2] for e in log if e[:2] == ("end", "up"))
    starts = {e[1]: e for e in log if e[0] == "start"}
    assert starts["x"][2] >= up_end and starts["y"][2] >= up_end
    assert starts["x"][3] == ["up"] and starts["y"][3] == ["up"]
    assert abs(starts["x"][2] - starts["y"][2]) < 0.05  # dependants fan out together
    assert all(r.status is TaskStatus.completed for r in res.values())


async def test_failure_keeps_completed_work_and_skips_dependants():
    log: list = []
    tasks = [spec("good"), spec("bad"), spec("needs_bad", ["bad"]), spec("any_ok", ["good", "bad"], "any")]
    res = await DAGScheduler(tasks, runner(log, 0.05, fail={"bad"}), concurrency=4,
                             cancel_event=asyncio.Event()).run()
    assert res["good"].status is TaskStatus.completed
    assert res["bad"].status is TaskStatus.failed
    assert res["needs_bad"].status is TaskStatus.skipped and "bad" in res["needs_bad"].error
    assert res["any_ok"].status is TaskStatus.completed
    any_start = next(e for e in log if e[:2] == ("start", "any_ok"))
    assert any_start[3] == ["good"]  # only completed upstream is passed on


async def test_internal_exception_becomes_failed_result():
    res = await DAGScheduler([spec("a"), spec("b")], runner([], 0.01, boom={"a"}), concurrency=2,
                             cancel_event=asyncio.Event()).run()
    assert res["a"].status is TaskStatus.failed and "kaboom" in res["a"].error
    assert res["b"].status is TaskStatus.completed


async def test_cancel_keeps_finished_results():
    log: list = []
    cancel = asyncio.Event()
    tasks = [spec("fast"), spec("slow"), spec("after", ["slow"])]

    async def run_one(t, upstream):
        await asyncio.sleep(0.05 if t.task_id == "fast" else 5)
        return AgentResult(task_id=t.task_id, agent_role="critic", status=TaskStatus.completed)

    async def cancel_soon():
        await asyncio.sleep(0.2)
        cancel.set()

    started = time.perf_counter()
    _, res = await asyncio.gather(cancel_soon(), DAGScheduler(tasks, run_one, concurrency=4,
                                                              cancel_event=cancel).run())
    assert time.perf_counter() - started < 1.0
    assert res["fast"].status is TaskStatus.completed
    assert res["slow"].status is TaskStatus.cancelled
    assert res["after"].status is TaskStatus.cancelled
