"""Crash recovery: simulate app death with live runs left in the DB, restart, verify."""

import sqlite3
import subprocess
import sys

import psutil
import pytest

from orchestrator.db import runs as runs_repo
from orchestrator.domain.states import AgentActivity, TaskState
from orchestrator.gitops import worktrees
from orchestrator.providers.mock import MockScript
from orchestrator.scheduler.recovery import recover
from orchestrator.testing import PASS_CHECK, make_harness, py_check
from orchestrator.util import now_iso

S = TaskState
OK = PASS_CHECK


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


async def _simulate_crash_mid_run(h, task, pid: int, *, worktree: bool = True) -> str:
    """Leave the DB exactly as a crashed orchestrator would: task RUNNING, run 'running' with a pid."""
    project = await h.projects.get(task.project_id)
    fields = {}
    if worktree:
        wt = worktrees.worktree_path(h.ctx.paths.worktrees_dir, task.id)
        from orchestrator.gitops import git

        base = git.rev_parse(project.path)
        info = worktrees.create(project.path, wt, task.id, base)
        (wt / "partial.txt").write_text("half done", encoding="utf-8")
        fields = {"worktree_path": info.path, "branch": info.branch, "base_commit": base}
    else:
        fields = {"worktree_path": project.path}
    await h.tasks.set_fields(task.id, fields)
    await h.tasks.transition(task.id, S.QUEUED)
    await h.tasks.transition(task.id, S.RUNNING)
    run_id = "run_CRASHED0000000000000000001"
    ts = now_iso()
    await h.ctx.db.write(
        lambda tx: runs_repo.insert(
            tx,
            {"id": run_id, "task_id": task.id, "phase": "execute", "agent_id": "agt_backend_1", "provider": "mock",
             "status": "running", "pid": pid, "usage": {}, "started_at": ts, "heartbeat_at": ts,
             "idempotency_key": f"{task.id}:execute:1"},
        )
    )
    await h.agents.set_activity("agt_backend_1", AgentActivity.CODING,
                                live=True, run_id=run_id, task_id=task.id, operation="execute")
    return run_id


async def test_restart_requeues_interrupted_run_without_duplicate(tmp_path):
    h = await make_harness(tmp_path)
    p = await h.project()
    task = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK)
    run_id = await _simulate_crash_mid_run(h, task, _dead_pid())
    h.ctx.close()  # "app dies": nothing else is cleaned up

    h2 = await make_harness(tmp_path)  # restart on the same data dir
    try:
        report = await recover(h2.ctx, h2.agents)
        assert report["interrupted_runs"] == [run_id]
        assert report["requeued"] == [task.id]
        t = await h2.tasks.get(task.id)
        assert t.state == S.QUEUED and "recovered" in t.state_reason
        run = await h2.ctx.db.read(lambda tx: runs_repo.get(tx, run_id))
        assert run.status.value == "interrupted"
        cps = await h2.executor.checkpoints.list(task.id)
        assert cps[-1].kind == "handoff" and cps[-1].git_ref  # partial work preserved in git
        events = await h2.ctx.bus.replay(0, types=["system.recovery"])
        assert len(events) == 1 and events[0].payload["requeued"] == [task.id]
        seat = await h2.agents.get("agt_backend_1")
        assert seat.activity.value == "idle"

        # The same idempotency key can never be inserted twice.
        with pytest.raises(sqlite3.IntegrityError):
            await h2.ctx.db.write(
                lambda tx: runs_repo.insert(
                    tx, {"id": "run_dup", "task_id": task.id, "phase": "execute", "provider": "mock", "status": "starting",
                         "usage": {}, "started_at": now_iso(), "idempotency_key": f"{task.id}:execute:1"},
                )
            )
        # Resume: a new attempt with a new key; the interrupted attempt is not re-executed.
        h2.mock.queue(MockScript.success())
        assert await h2.run_once(task.id) == S.COMPLETED
        runs = await h2.ctx.db.read(lambda tx: runs_repo.for_task(tx, task.id))
        keys = [r.idempotency_key for r in runs]
        assert keys == [f"{task.id}:execute:1", f"{task.id}:execute:2"]
        assert len(h2.mock.requests) == 1
        assert "partial.txt" in h2.mock.requests[0].prompt or "interrupted" in h2.mock.requests[0].prompt

        # A second restart finds nothing to recover.
        again = await recover(h2.ctx, h2.agents)
        assert again["interrupted_runs"] == [] and again["requeued"] == []
    finally:
        await h2.close()


async def test_in_place_crash_blocks_task(tmp_path):
    h = await make_harness(tmp_path)
    p = await h.project("plain", git_repo=False)
    task = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK)
    await _simulate_crash_mid_run(h, task, _dead_pid(), worktree=False)
    h.ctx.close()
    h2 = await make_harness(tmp_path)
    try:
        report = await recover(h2.ctx, h2.agents)
        assert report["blocked"] == [task.id]
        t = await h2.tasks.get(task.id)
        assert t.state == S.BLOCKED and "side effects uncertain" in t.state_reason
    finally:
        await h2.close()


async def test_alive_recorded_pid_is_preserved_and_task_blocks(tmp_path):
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        h = await make_harness(tmp_path)
        p = await h.project()
        task = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK)
        run_id = await _simulate_crash_mid_run(h, task, orphan.pid)
        h.ctx.close()
        h2 = await make_harness(tmp_path)
        try:
            report = await recover(h2.ctx, h2.agents, own_pid=-1)
            assert report["killed_orphans"] == []
            assert orphan.poll() is None
            assert (await h2.tasks.get(task.id)).state == S.BLOCKED
        finally:
            await h2.close()
    finally:
        if orphan.poll() is None:
            orphan.kill()
        orphan.wait(timeout=10)


async def test_stale_worktree_reported(tmp_path):
    h = await make_harness(tmp_path)
    try:
        p = await h.project()
        h.mock.queue(MockScript.success())
        t = await h.task(p.id, acceptance_criteria=["x"], verify_commands=OK, autostart=True)
        assert await h.run_once(t.id) == S.COMPLETED
        report = await recover(h.ctx, h.agents)
        assert any(t.id in w for w in report["stale_worktrees"])
    finally:
        await h.close()
