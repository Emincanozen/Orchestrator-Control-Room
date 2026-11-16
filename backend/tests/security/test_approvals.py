import pytest

from orchestrator.db import records
from orchestrator.domain.states import TaskState
from orchestrator.errors import Conflict
from orchestrator.security import approvals

S = TaskState


async def _task_needing_approval(h, kind="destructive_git"):
    p = await h.project()
    t = await h.task(p.id, acceptance_criteria=["x"], autostart=True)

    def fn(tx):
        from orchestrator.services.tasks import transition_tx

        transition_tx(h.ctx, tx, t.id, S.NEEDS_APPROVAL, reason="gate")
        return approvals.request_tx(h.ctx, tx, kind, "force push?", task_id=t.id, project_id=p.id)

    a = await h.ctx.db.write(fn)
    return t, a


async def test_approve_requeues(h):
    t, a = await _task_needing_approval(h)
    assert a.status == "pending"
    resolved = await h.approvals.resolve(a.id, True, note="ok")
    assert resolved.status == "approved" and resolved.resolved_by == "user"
    assert (await h.tasks.get(t.id)).state == S.QUEUED
    types = [e.type for e in await h.ctx.bus.replay(0, task_id=t.id)]
    assert "approval.requested" in types and "approval.resolved" in types


async def test_deny_blocks_with_reason(h):
    t, a = await _task_needing_approval(h)
    await h.approvals.resolve(a.id, False, note="too risky")
    t = await h.tasks.get(t.id)
    assert t.state == S.BLOCKED
    assert t.state_reason == "denied: destructive_git: too risky"


async def test_double_resolve_rejected(h):
    _t, a = await _task_needing_approval(h)
    await h.approvals.resolve(a.id, True)
    with pytest.raises(Conflict):
        await h.approvals.resolve(a.id, False)


async def test_duplicate_request_returns_open_approval(h):
    t, a = await _task_needing_approval(h)
    again = await h.ctx.db.write(lambda tx: approvals.request_tx(h.ctx, tx, "destructive_git", "again", task_id=t.id))
    assert again.id == a.id


async def test_auto_approve_policy(h):
    await h.ctx.db.write(lambda tx: records.setting_put(tx, "approvals", {"auto_approve": ["unverifiable_completion"]}))
    p = await h.project()
    t = await h.task(p.id, acceptance_criteria=["x"], autostart=True)  # no verify commands
    assert await h.run_once(t.id) == S.COMPLETED
    [a] = await h.approvals.list(task_id=t.id)
    assert a.status == "approved" and a.resolved_by == "policy:auto_approve"
    v = await h.ctx.db.read(lambda tx: records.latest_verification(tx, t.id))
    assert v.status == "unverifiable_accepted"
