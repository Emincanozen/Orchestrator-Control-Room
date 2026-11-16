"""A stale PID is not authority to terminate a user's current process."""
import subprocess
import sys
import json
import os
from types import SimpleNamespace

import psutil
import pytest

from orchestrator.db import runs as runs_repo
from orchestrator.domain.states import TaskState
from orchestrator.gitops import git, worktrees
from orchestrator.providers.mock import MockScript
from orchestrator.scheduler import recovery
from orchestrator.testing import make_harness
from orchestrator.security import worker_job
from orchestrator.util import now_iso, parse_iso
from recovery.test_recovery import _simulate_crash_mid_run


@pytest.mark.parametrize("owned_worktree", [True, False])
async def test_recovery_preserves_unverified_live_process_and_blocks_task(h, owned_worktree):
    # This is OUR harmless control process. Only the test's retained Popen
    # handle may terminate it; production recovery must not trust its stored PID.
    control = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        project = await h.project()
        task = await h.task(project.id, verify_commands=[{"file_exists": "partial.txt"}])
        run_id = await _simulate_crash_mid_run(h, task, control.pid, worktree=owned_worktree)
        report = await recovery.recover(h.ctx, h.agents)
        assert control.poll() is None, "recovery terminated an unverified process"
        assert report["killed_orphans"] == [] and report["requeued"] == []
        assert report["blocked"] == [task.id]
        assert (await h.tasks.get(task.id)).state_reason.startswith("security:")
        assert (await h.ctx.db.read(lambda tx: runs_repo.get(tx, run_id))).status.value == "interrupted"
        assert not await h.executor.checkpoints.list(task.id)
        assert git.rev_parse(project.path), "source repository must not be quarantined"
        if owned_worktree:
            current = await h.tasks.get(task.id)
            with pytest.raises(git.QuarantinedWorktree):
                git.rev_parse(current.worktree_path)
            h.mock.queue(MockScript.success("must not execute"))
            await h.scheduler.retry_task(task.id)
            assert await h.run_once(task.id) == TaskState.BLOCKED
            assert not h.mock.requests
    finally:
        if control.poll() is None:
            control.kill()
        control.wait(timeout=10)


async def test_owned_quarantine_requires_cold_restart_after_process_is_gone(tmp_path):
    control = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(180)"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    h = await make_harness(tmp_path)
    try:
        project = await h.project()
        task = await h.task(project.id, acceptance_criteria=["result exists"], verify_commands=[{"file_exists": "result.txt"}])
        await _simulate_crash_mid_run(h, task, control.pid)
        await recovery.recover(h.ctx, h.agents)
        control.kill()
        control.wait(timeout=10)
        await h.scheduler.retry_task(task.id)
        assert await h.run_once(task.id) == TaskState.BLOCKED  # PID gone, same-server quarantine stays
        assert not h.mock.requests
    finally:
        await h.close()
        if control.poll() is None:
            control.kill()
        control.wait(timeout=10)
    code = '''
import asyncio,json,sys
from pathlib import Path
from orchestrator.testing import make_harness
from orchestrator.scheduler.recovery import recover
from orchestrator.providers.mock import MockScript
async def main():
 h=await make_harness(Path(sys.argv[1]))
 try:
  await recover(h.ctx,h.agents)
  h.mock.queue(MockScript.success('ok',files={'result.txt':'owned output'}))
  await h.scheduler.retry_task(sys.argv[2])
  state=await h.run_once(sys.argv[2])
  print(json.dumps({'state':state.value,'mock_calls':len(h.mock.requests)}))
 finally: await h.close()
asyncio.run(main())
'''
    result = worker_job.run_confined([sys.executable, "-c", code, str(tmp_path), task.id],
                                    env=dict(os.environ), timeout=45, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"state": "COMPLETED", "mock_calls": 1}


async def test_quarantine_refuses_a_different_tasks_path(h):
    project = await h.project()
    other = await h.task(project.id)
    path = worktrees.worktree_path(h.ctx.paths.worktrees_dir, other.id)
    worktrees.create(project.path, path, other.id, git.rev_parse(project.path))
    forged = SimpleNamespace(id="different-task", worktree_path=str(path))
    assert not recovery._quarantine_owned_task(h.ctx, forged, project)
    assert git.rev_parse(path) and git.rev_parse(project.path)


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction rejection")
async def test_quarantine_refuses_junction_to_another_task(h):
    project = await h.project()
    other = await h.task(project.id)
    target = worktrees.worktree_path(h.ctx.paths.worktrees_dir, other.id)
    worktrees.create(project.path, target, other.id, git.rev_parse(project.path))
    alias = worktrees.worktree_path(h.ctx.paths.worktrees_dir, "junction-alias")
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
                   capture_output=True, check=True)
    try:
        forged = SimpleNamespace(id="junction-alias", worktree_path=str(alias))
        assert not recovery._quarantine_owned_task(h.ctx, forged, project)
        assert git.rev_parse(target) and git.rev_parse(project.path)
    finally:
        alias.rmdir()  # only this newly created junction, never its target


@pytest.mark.parametrize("case", ["older", "reuse_within_window", "reuse_after_window", "access_denied", "gone"])
def test_pid_probe_only_proves_liveness_never_ownership(monkeypatch, case):
    started = now_iso()
    timestamp = parse_iso(started).timestamp()
    def process(pid):
        if case == "access_denied":
            raise psutil.AccessDenied(pid)
        if case == "gone":
            raise psutil.NoSuchProcess(pid)
        return SimpleNamespace(is_running=lambda: True, status=lambda: "running",
            create_time=lambda: timestamp + {"older": -300, "reuse_within_window": 1, "reuse_after_window": 121}[case])
    fake = SimpleNamespace(Process=process, Error=psutil.Error, NoSuchProcess=psutil.NoSuchProcess,
                           STATUS_ZOMBIE=psutil.STATUS_ZOMBIE)
    monkeypatch.setattr(recovery, "psutil", fake)
    run = SimpleNamespace(pid=54321, started_at=started)
    assert recovery.pid_alive_for_run(run, own_pid=99999) is (case != "gone")


async def test_uncertainty_survives_restart_and_retry_until_process_is_gone(tmp_path, monkeypatch):
    control = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(180)"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    h = None
    try:
        h = await make_harness(tmp_path)
        project = await h.project()
        task = await h.task(project.id, verify_commands=[{"file_exists": "result.txt"}])
        await _simulate_crash_mid_run(h, task, control.pid, worktree=False)
        await recovery.recover(h.ctx, h.agents)
        assert (await h.tasks.get(task.id)).state == TaskState.BLOCKED
        await h.close()
        h = await make_harness(tmp_path)
        await recovery.recover(h.ctx, h.agents)  # run is already INTERRUPTED
        def forbidden(*args, **kwargs):
            raise AssertionError("retry reached worktree creation before the persistent PID barrier")
        with monkeypatch.context() as patch:
            patch.setattr(recovery.worktrees, "create", forbidden)
            await h.scheduler.retry_task(task.id)
            assert await h.run_once(task.id) == TaskState.BLOCKED
        assert not h.mock.requests and control.poll() is None
        assert git.rev_parse(project.path), "unverified source paths must not be quarantined"
        control.kill()  # exact Popen handle created by this test, never a DB PID
        control.wait(timeout=10)
        await h.close()
        h = await make_harness(tmp_path)
        await recovery.recover(h.ctx, h.agents)
        h.mock.queue(MockScript.success("ok", files={"result.txt": "owned output"}))
        await h.scheduler.retry_task(task.id)
        assert await h.run_once(task.id) == TaskState.COMPLETED
        # A later reuse of the dead historical PID must not revive a resolved
        # barrier or quarantine completed/new work on a future restart.
        monkeypatch.setattr(recovery, "pid_alive_for_run", lambda *args: True)
        assert not await recovery.has_unverified_live_process(h.ctx, task.id)
        assert not (await recovery.recover(h.ctx, h.agents))["quarantined"]
    finally:
        if h:
            await h.close()
        if control.poll() is None:
            control.kill()
        control.wait(timeout=10)
