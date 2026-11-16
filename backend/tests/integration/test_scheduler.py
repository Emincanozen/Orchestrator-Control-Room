"""Scheduler: concurrency, dependency ordering, worktree exclusion, controls, error mapping."""

import asyncio

from orchestrator.db import runs as runs_repo
from orchestrator.domain.models import TaskUpdate
from orchestrator.domain.states import ACTIVE_STATES, TaskState
from orchestrator.providers.base import ErrorCategory
from orchestrator.providers.mock import MockScript, MockStep
from orchestrator.testing import FAIL_CHECK, PASS_CHECK, py_check

S = TaskState
OK = PASS_CHECK


async def _wait_live_run(h, task_id):
    for _ in range(500):
        runs = await h.ctx.db.read(lambda tx: runs_repo.for_task(tx, task_id))
        live = [r for r in runs if h.mock.is_running(r.id)]
        if live:
            return live[0]
        await asyncio.sleep(0.02)
    raise AssertionError("run never became live")


async def _max_active(h, task_ids, until_done: float = 30.0, *, release=None) -> int:
    """Sample how many of task_ids are simultaneously active until all are terminal/failed."""
    peak = 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + until_done
    while loop.time() < deadline:
        states = [(await h.tasks.get(t)).state for t in task_ids]
        peak = max(peak, sum(1 for s in states if s in ACTIVE_STATES))
        if release:
            await release(states)
        if all(s in (S.COMPLETED, S.FAILED, S.CANCELLED) for s in states):
            return peak
        await asyncio.sleep(0.02)
    raise AssertionError(f"tasks did not finish: {states}")


async def test_global_concurrency_limit(h):
    await h.set_limits(max_parallel_workers=2, max_parallel_per_project=5)
    p = await h.project()
    h.mock._default = MockScript(steps=[MockStep(action="sleep", seconds=0.4), MockStep(action="succeed")])
    ids = [(await h.task(p.id, title=f"t{i}", acceptance_criteria=["x"], verify_commands=OK, autostart=True)).id for i in range(5)]
    await h.scheduler.start()
    peak = await _max_active(h, ids)
    assert peak == 2
    assert [(await h.tasks.get(t)).state for t in ids] == [S.COMPLETED] * 5


async def test_per_project_limit(h):
    await h.set_limits(max_parallel_workers=5, max_parallel_per_project=1)
    p1, p2 = await h.project("a"), await h.project("b")
    h.mock._default = MockScript(steps=[MockStep(action="sleep", seconds=0.3), MockStep(action="succeed")])
    a = [(await h.task(p1.id, title=f"a{i}", acceptance_criteria=["x"], verify_commands=OK, autostart=True)).id for i in range(3)]
    b = [(await h.task(p2.id, title=f"b{i}", acceptance_criteria=["x"], verify_commands=OK, autostart=True)).id for i in range(2)]
    await h.scheduler.start()
    peak_a = 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 30
    peak_total = 0
    while loop.time() < deadline:
        sa = [(await h.tasks.get(t)).state for t in a]
        sb = [(await h.tasks.get(t)).state for t in b]
        peak_a = max(peak_a, sum(s in ACTIVE_STATES for s in sa))
        peak_total = max(peak_total, sum(s in ACTIVE_STATES for s in sa + sb))
        if all(s == S.COMPLETED for s in sa + sb):
            break
        await asyncio.sleep(0.02)
    assert peak_a == 1
    assert peak_total == 2


async def test_dependency_ordering(h):
    await h.set_limits(max_parallel_workers=3)
    p = await h.project()
    h.mock._default = MockScript(steps=[MockStep(action="sleep", seconds=0.2), MockStep(action="succeed")])
    a = await h.task(p.id, title="a", priority=10, acceptance_criteria=["x"], verify_commands=OK)
    b = await h.task(p.id, title="b", priority=90, acceptance_criteria=["x"], verify_commands=OK, depends_on=[a.id])
    c = await h.task(p.id, title="c", priority=99, acceptance_criteria=["x"], verify_commands=OK, depends_on=[b.id])
    for t in (c, b, a):
        await h.scheduler.start_task(t.id)
    await h.scheduler.start()
    await h.wait_state(c.id, [S.COMPLETED])
    order = []
    for e in await h.ctx.bus.replay(0, types=["run.started"], limit=100):
        order.append(e.task_id)
    assert order == [a.id, b.id, c.id]
    ta, tb = await h.tasks.get(a.id), await h.tasks.get(b.id)
    assert ta.finished_at <= tb.started_at


async def test_failed_dependency_blocks_dependent(h):
    await h.set_limits(max_repair_cycles=0)
    p = await h.project()
    a = await h.task(p.id, title="a", acceptance_criteria=["x"], verify_commands=FAIL_CHECK, autostart=True)
    b = await h.task(p.id, title="b", acceptance_criteria=["x"], verify_commands=OK, depends_on=[a.id], autostart=True)
    await h.scheduler.start()
    await h.wait_state(a.id, [S.FAILED])
    tb = await h.wait_state(b.id, [S.BLOCKED])
    assert tb.state_reason.startswith("dependency ")
    # retrying the dependency unblocks the dependent once it is no longer failed
    h.mock.queue(MockScript.success())
    await h.tasks.update(a.id, TaskUpdate(verify_commands=OK))
    await h.scheduler.retry_task(a.id)
    await h.wait_state(b.id, [S.COMPLETED])


async def test_non_git_tasks_all_block_without_starting_workers(h):
    await h.set_limits(max_parallel_workers=3, max_parallel_per_project=3)
    p = await h.project("plain", git_repo=False)
    h.mock._default = MockScript(steps=[MockStep(action="sleep", seconds=0.3), MockStep(action="succeed")])
    ids = [(await h.task(p.id, title=f"t{i}", acceptance_criteria=["x"], verify_commands=OK, autostart=True)).id for i in range(3)]
    await h.scheduler.start()
    for task_id in ids:
        await h.wait_state(task_id, [S.BLOCKED])
    assert not h.mock.requests


async def test_cancel_kills_hanging_run(h):
    p = await h.project()
    h.mock.queue(MockScript.hang())
    t = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK, autostart=True)
    await h.scheduler.start()
    await _wait_live_run(h, t.id)
    await h.scheduler.cancel_task(t.id)
    t2 = await h.wait_state(t.id, [S.CANCELLED], timeout=10)
    assert t2.state_reason == "cancelled by user"
    runs = await h.ctx.db.read(lambda tx: runs_repo.for_task(tx, t.id))
    assert runs[0].status.value == "cancelled"
    assert set(h.mock.cancelled) == {runs[0].id}
    agents = await h.agents.list()
    assert all(a.activity.value in ("idle",) for a in agents)


async def test_pause_and_resume(h):
    p = await h.project()
    h.mock.queue(MockScript.hang(), MockScript.success())
    t = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK, autostart=True)
    await h.scheduler.start()
    await _wait_live_run(h, t.id)
    await h.scheduler.pause_task(t.id)
    await h.wait_state(t.id, [S.PAUSED], timeout=10)
    await h.scheduler.resume_task(t.id)
    await h.wait_state(t.id, [S.COMPLETED])
    runs = await h.ctx.db.read(lambda tx: runs_repo.for_task(tx, t.id))
    assert [r.status.value for r in runs] == ["cancelled", "succeeded"]
    assert [r.idempotency_key.split(":", 1)[1] for r in runs] == ["execute:1", "execute:2"]


async def test_quota_error_waits_then_resumes(h):
    p = await h.project()
    h.mock.queue(MockScript.failure(ErrorCategory.QUOTA_EXHAUSTED, "usage limit reached", status="rejected"), MockScript.success())
    t = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK, autostart=True)
    assert await h.run_once(t.id) == S.WAITING_FOR_QUOTA
    t = await h.tasks.get(t.id)
    assert t.not_before is not None and "quota_exhausted" in t.state_reason
    assert t.retry_count == 0  # quota waits are not failures
    agents = {a.id: a for a in await h.agents.list()}
    assert any(a.activity.value == "rate_limited" and a.current_task_id == t.id for a in agents.values())
    await h.scheduler.tick()
    assert (await h.tasks.get(t.id)).state == S.WAITING_FOR_QUOTA  # not yet
    await h.ctx.db.write(lambda tx: tx.execute("UPDATE tasks SET not_before = '2000-01-01T00:00:00+00:00' WHERE id = ?", (t.id,)))
    await h.scheduler.start()
    await h.wait_state(t.id, [S.COMPLETED])


async def test_rate_limit_reset_time_used(h):
    p = await h.project()
    h.mock.queue(MockScript.failure(ErrorCategory.RATE_LIMIT, "slow down", resets_at="2999-01-01T00:00:00+00:00"))
    t = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK, autostart=True)
    assert await h.run_once(t.id) == S.WAITING_FOR_QUOTA
    t = await h.tasks.get(t.id)
    assert not t.not_before.startswith("2999")  # implausible reset capped to recheck interval


async def test_auth_error_blocks(h):
    p = await h.project()
    h.mock.queue(MockScript.failure(ErrorCategory.AUTH, "not logged in"))
    t = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK, autostart=True)
    assert await h.run_once(t.id) == S.BLOCKED


async def test_permission_denied_needs_approval(h):
    p = await h.project()
    h.mock.queue(MockScript.failure(ErrorCategory.PERMISSION_DENIED, "needs network"), MockScript.success())
    t = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK, autostart=True)
    assert await h.run_once(t.id) == S.NEEDS_APPROVAL
    [a] = await h.approvals.list("pending", t.id)
    assert a.kind == "permission_escalation"
    await h.approvals.resolve(a.id, True)
    assert (await h.tasks.get(t.id)).state == S.QUEUED
    assert await h.run_once(t.id) == S.COMPLETED


async def test_transient_errors_retry_then_fail(h):
    await h.set_limits(max_retry_attempts=2, max_consecutive_failures=10)
    p = await h.project()
    h.mock._default = MockScript.crash()
    t = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK, autostart=True)
    await h.scheduler.start()
    t = await h.wait_state(t.id, [S.FAILED])
    runs = await h.ctx.db.read(lambda tx: runs_repo.for_task(tx, t.id))
    assert len(runs) == 3 and all(r.error_category == "crashed" for r in runs)
    assert "retry limit" in t.state_reason


async def test_adapter_exception_is_contained(h):
    await h.set_limits(max_retry_attempts=0)
    p = await h.project()
    h.mock.queue(MockScript(steps=[MockStep(action="raise", text="bug in adapter")]))
    t = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK, autostart=True)
    assert await h.run_once(t.id) == S.FAILED
    runs = await h.ctx.db.read(lambda tx: runs_repo.for_task(tx, t.id))
    assert runs[0].status.value == "failed" and "bug in adapter" in runs[0].error_message


async def test_silence_timeout_kills_run(h):
    await h.set_limits(run_heartbeat_timeout_seconds=0.5, max_retry_attempts=0)
    p = await h.project()
    h.mock.queue(MockScript.hang())
    t = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK, autostart=True)
    assert await h.run_once(t.id) == S.FAILED
    runs = await h.ctx.db.read(lambda tx: runs_repo.for_task(tx, t.id))
    assert runs[0].error_category == "timeout"
    assert set(h.mock.cancelled) == {runs[0].id}


async def test_no_provider_blocks(h):
    p = await h.project()
    h.providers.clear()
    t = await h.task(p.id, preferred_provider=None, acceptance_criteria=["x"], verify_commands=OK, autostart=True)
    assert await h.run_once(t.id) == S.BLOCKED
    assert "no suitable provider" in (await h.tasks.get(t.id)).state_reason


async def test_graceful_shutdown_requeues_running_task(h):
    p = await h.project()
    h.mock.queue(MockScript.hang())
    t = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK, autostart=True)
    await h.scheduler.start()
    await _wait_live_run(h, t.id)
    await h.scheduler.stop(grace_seconds=10)
    t = await h.tasks.get(t.id)
    assert t.state == S.QUEUED
    runs = await h.ctx.db.read(lambda tx: runs_repo.for_task(tx, t.id))
    assert runs[0].status.value == "interrupted"
