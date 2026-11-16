import asyncio

import pytest

from orchestrator.db import tasks as tasks_repo
from orchestrator.domain import events as ev
from orchestrator.domain.models import TaskUpdate
from orchestrator.domain.states import TaskState
from orchestrator.errors import DependencyCycle, ValidationFailed
from orchestrator.services import tasks as task_service
from orchestrator.services.tasks import find_cycle, transition_tx

S = TaskState


async def test_transition_and_event_are_atomic(h, monkeypatch):
    p = await h.project()
    task = await h.task(p.id)
    real_append = h.ctx.bus.append

    def failing_append(tx, type, **kw):
        if type == ev.TASK_STATE_CHANGED:
            raise RuntimeError("event store down")
        return real_append(tx, type, **kw)

    monkeypatch.setattr(h.ctx.bus, "append", failing_append)
    async with h.ctx.bus.subscribe() as sub:
        with pytest.raises(RuntimeError):
            await h.tasks.transition(task.id, S.QUEUED)
        with pytest.raises(asyncio.TimeoutError):
            await sub.get(timeout=0.2)  # nothing broadcast for a rolled-back tx
    t = await h.tasks.get(task.id)
    assert t.state == S.CREATED
    rows = await h.ctx.db.read(lambda tx: tasks_repo.transitions(tx, task.id))
    assert [r.to_state for r in rows] == ["CREATED"]


async def test_event_broadcast_after_commit(h):
    p = await h.project()
    task = await h.task(p.id)
    async with h.ctx.bus.subscribe() as sub:
        await h.tasks.transition(task.id, S.QUEUED, reason="go")
        e = await sub.get(timeout=2)
    assert e.type == ev.TASK_STATE_CHANGED
    assert e.payload == {"from": "CREATED", "to": "QUEUED", "reason": "go", "actor": "system"}
    stored = await h.ctx.bus.replay(e.seq - 1, limit=1)
    assert stored[0].seq == e.seq


def test_find_cycle():
    assert find_cycle({"a": ["b"], "b": ["c"], "c": ["a"]}, "a") == ["a", "b", "c", "a"]
    assert find_cycle({"a": ["b"], "b": ["c"]}, "a") is None
    assert find_cycle({"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}, "a") is None


async def test_dependency_cycles_rejected(h):
    p = await h.project()
    a = await h.task(p.id, title="a")
    b = await h.task(p.id, title="b", depends_on=[a.id])
    c = await h.task(p.id, title="c", depends_on=[b.id])
    assert c.depends_on == [b.id]
    with pytest.raises(DependencyCycle):
        await h.tasks.update(a.id, TaskUpdate(depends_on=[c.id]))  # a->c->b->a
    with pytest.raises(DependencyCycle):
        await h.tasks.update(a.id, TaskUpdate(depends_on=[a.id]))
    with pytest.raises(ValidationFailed):
        await h.tasks.update(a.id, TaskUpdate(depends_on=["tsk_missing"]))
    assert (await h.tasks.get(a.id)).depends_on == []
    # diamond is fine
    d = await h.task(p.id, title="d", depends_on=[b.id, c.id])
    assert sorted(d.depends_on) == sorted([b.id, c.id])


async def test_active_seconds_counts_only_active_states(h):
    p = await h.project()
    task = await h.task(p.id)
    await h.tasks.transition(task.id, S.QUEUED)
    await asyncio.sleep(0.3)  # queued time must not count
    await h.tasks.transition(task.id, S.RUNNING)
    await asyncio.sleep(0.25)
    await h.tasks.transition(task.id, S.TESTING)
    await asyncio.sleep(0.15)
    t = await h.tasks.transition(task.id, S.FAILED)
    assert 0.35 <= t.active_seconds < 0.8, t.active_seconds
    assert t.started_at is not None and t.finished_at is not None
    await asyncio.sleep(0.2)
    t = await h.tasks.transition(task.id, S.QUEUED)
    assert t.finished_at is None
    assert t.active_seconds < 0.8


async def test_stale_state_guard(h):
    p = await h.project()
    task = await h.task(p.id)
    with pytest.raises(task_service.StaleState):
        await h.tasks.transition(task.id, S.QUEUED, expect_from=[S.PAUSED])
    await h.ctx.db.write(lambda tx: transition_tx(h.ctx, tx, task.id, S.QUEUED, expect_from=[S.CREATED]))
