"""Crash in TESTING persists a human-review barrier, not automatic requeue."""
from orchestrator.db import runs as runs_repo
from orchestrator.domain.states import TaskState
from orchestrator.scheduler.recovery import recover
from orchestrator.security.approvals import SECURITY_REASON_PREFIX
from orchestrator.testing import PASS_CHECK, make_harness
from orchestrator.util import now_iso
from recovery.test_recovery import _simulate_crash_mid_run

S = TaskState
OK = PASS_CHECK





async def _crash_during_testing(h, task) -> str:
    """Provider run already succeeded, task left in TESTING, no PID recorded."""
    run_id = await _simulate_crash_mid_run(h, task, None)
    await h.ctx.db.write(lambda tx: runs_repo.update(
        tx, run_id, {"status": "succeeded", "finished_at": now_iso()}))
    await h.tasks.transition(task.id, S.TESTING)
    return run_id


async def test_recovery_blocks_task_interrupted_in_testing(tmp_path):
    h = await make_harness(tmp_path)
    p = await h.project()
    task = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK)
    run_id = await _crash_during_testing(h, task)
    h.ctx.close()

    h2 = await make_harness(tmp_path)
    try:
        report = await recover(h2.ctx, h2.agents)
        assert report["requeued"] == [] and report["blocked"] == [task.id]
        assert report["interrupted_runs"] == []
        t = await h2.tasks.get(task.id)
        assert t.state == S.BLOCKED
        assert SECURITY_REASON_PREFIX in t.state_reason
        assert "verification interrupted" in t.state_reason
        run = await h2.ctx.db.read(lambda tx: runs_repo.get(tx, run_id))
        assert run.status.value == "succeeded"
        cps = await h2.executor.checkpoints.list(task.id)
        assert not any(cp.kind == "handoff" for cp in cps)
    finally:
        await h2.close()


async def test_second_cold_recovery_keeps_testing_crash_blocked(tmp_path):
    h = await make_harness(tmp_path)
    p = await h.project()
    task = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK)
    await _crash_during_testing(h, task)
    h.ctx.close()
    h2 = await make_harness(tmp_path)
    await recover(h2.ctx, h2.agents)
    reason = (await h2.tasks.get(task.id)).state_reason
    h2.ctx.close()

    h3 = await make_harness(tmp_path)
    try:
        report = await recover(h3.ctx, h3.agents)
        assert task.id not in report["requeued"] and task.id not in report["blocked"]
        t = await h3.tasks.get(task.id)
        assert t.state == S.BLOCKED and t.state_reason == reason
        cps = await h3.executor.checkpoints.list(task.id)
        assert not any(cp.kind == "handoff" for cp in cps)
    finally:
        await h3.close()
