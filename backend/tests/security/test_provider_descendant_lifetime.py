"""Native provider descendants must be gone before worktree ownership is released."""

import asyncio
import json
import os
import sys
import time

import psutil
import pytest

from orchestrator.providers.process import run_capture, spawn_process
from orchestrator.security import worker_job

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows nested jobs")


@pytest.mark.parametrize("helper", [False, True], ids=["provider", "helper"])
@pytest.mark.parametrize("redirect", [False, True], ids=["inherited-pipes", "redirected-pipes"])
def test_successful_parent_exit_drains_detached_child(tmp_path, helper, redirect):
    pidfile = tmp_path / "child.json"
    child_code = ("import json,os,psutil,time; "
                  f"open({str(pidfile)!r},'w').write(json.dumps([os.getpid(),psutil.Process().create_time()])); "
                  "time.sleep(60)")
    parent_code = ("import subprocess,sys,time,pathlib; "
                   f"subprocess.Popen([sys.executable,'-c',{child_code!r}],"
                   + ("stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                      if redirect else "stdin=subprocess.DEVNULL); ")
                   +
                   f"p=pathlib.Path({str(pidfile)!r}); "
                   "\nfor _ in range(100):"
                   "\n if p.exists(): break"
                   "\n time.sleep(.02)"
                   "\nassert p.exists(); print('PARENT-DONE')")

    async def run():
        if helper:
            code, out, err = await run_capture([sys.executable, "-c", parent_code], cwd=str(tmp_path),
                                               env=dict(os.environ), timeout=15)
            assert code == 0 and "PARENT-DONE" in out, err
            return
        handle = await spawn_process([sys.executable, "-c", parent_code], cwd=str(tmp_path),
                                     env=dict(os.environ), timeout=15)
        async for _ in handle.stream():
            pass
        assert await handle.wait() == 0

    try:
        asyncio.run(asyncio.wait_for(run(), 12))
        pid, created = json.loads(pidfile.read_text())
        deadline = time.monotonic() + 2
        while psutil.pid_exists(pid) and time.monotonic() < deadline:
            time.sleep(.02)
        assert not psutil.pid_exists(pid), "provider descendant survived successful parent exit"
    finally:
        if pidfile.exists():
            pid, created = json.loads(pidfile.read_text())
            try:
                child = psutil.Process(pid)
                if child.create_time() == created:
                    child.kill()  # only this test's confirmed PID, never a process-name kill
                    child.wait(5)
            except psutil.NoSuchProcess:
                pass


def test_job_query_failure_still_terminates_and_retains_unverified_handle(monkeypatch):
    job = worker_job.SpawnJob()
    terminate = worker_job._k32.TerminateJobObject
    queried = job._active_processes
    calls = []

    def terminate_spy(handle, code):
        calls.append(handle)
        return terminate(handle, code)

    def unavailable():
        raise worker_job.ContainmentError("injected accounting query failure")

    try:
        monkeypatch.setattr(worker_job._k32, "TerminateJobObject", terminate_spy)
        monkeypatch.setattr(job, "_active_processes", unavailable)
        with pytest.raises(worker_job.ContainmentError, match="accounting query failure"):
            job.close()
        assert calls, "query failure must never skip attempted worker termination"
        assert job._handle and not job.drained and job in worker_job._retained_jobs
    finally:
        monkeypatch.setattr(job, "_active_processes", queried)
        monkeypatch.setattr(worker_job._k32, "TerminateJobObject", terminate)
        job.close()


def test_cancel_during_async_creation_reaps_still_suspended_child(tmp_path, monkeypatch):
    from orchestrator.providers import process as process_module

    marker = tmp_path / "must-not-run"
    created_pid = []
    created_time = []
    baseline = set(worker_job._retained_jobs)

    async def run():
        created, release = asyncio.Event(), asyncio.Event()
        real_spawn = asyncio.create_subprocess_exec

        async def delayed_spawn(*args, **kwargs):
            proc = await real_spawn(*args, **kwargs)
            created_pid.append(proc.pid)
            created_time.append(psutil.Process(proc.pid).create_time())
            created.set()
            await release.wait()
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
        pending = asyncio.create_task(spawn_process(
            [sys.executable, "-c", f"open({str(marker)!r},'w').write('ran')"],
            cwd=str(tmp_path), env=dict(os.environ), timeout=15))
        await asyncio.wait_for(created.wait(), 5)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 5)
        assert not process_module._pending_spawns

    try:
        asyncio.run(run())
        assert created_pid and not psutil.pid_exists(created_pid[0])
        assert not marker.exists()
        assert set(worker_job._retained_jobs) == baseline
    finally:
        if created_pid:
            try:
                proc = psutil.Process(created_pid[0])
                if proc.create_time() == created_time[0]:
                    proc.kill()
                    proc.wait(5)
            except psutil.NoSuchProcess:
                pass
