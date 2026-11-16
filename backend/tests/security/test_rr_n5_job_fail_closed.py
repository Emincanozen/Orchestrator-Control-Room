"""Re-review N5: when the worker job cannot be created or a process cannot be assigned to it, nothing may run
unconfined. The spawn fails (the still-suspended process is killed) and the app refuses to start without a job.

The failure is injected by replacing the assign function (`worker_job._assign_windows`) / the job creation
(`worker_job._ensure_job`): the one seam this needs."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.providers.mock import MockProvider
from orchestrator.providers.process import run_capture, spawn_process
from orchestrator.scheduler.verification import run_command
from orchestrator.security import worker_job

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows-only")


@pytest.fixture()
def failing_assign(monkeypatch):
    pids: list[int] = []

    def fail(pid: int) -> None:
        pids.append(pid)
        raise OSError(5, "simulated AssignProcessToJobObject failure")

    monkeypatch.setattr(worker_job, "_assign_windows", fail)
    return pids


def _gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


def _marker_code(marker: Path) -> str:
    return f"open(r'{marker}', 'w').write('ran')"


@windows_only
def test_provider_spawn_fails_and_never_runs_when_assignment_fails(failing_assign, tmp_path: Path):
    marker = tmp_path / "ran"

    async def go() -> None:
        with pytest.raises(OSError):
            await spawn_process([sys.executable, "-c", _marker_code(marker)], cwd=str(tmp_path), env=dict(os.environ), timeout=30)

    asyncio.run(go())
    assert failing_assign, "assignment was not attempted"
    assert _gone(failing_assign[0])
    time.sleep(0.3)
    assert not marker.exists(), "the process ran unconfined"
    assert failing_assign[0] not in worker_job._tracked  # no POSIX-style PID tracking fallback on Windows


@windows_only
def test_helper_run_capture_fails_closed(failing_assign, tmp_path: Path):
    marker = tmp_path / "ran"
    code, _out, err = asyncio.run(run_capture([sys.executable, "-c", _marker_code(marker)], timeout=30))
    assert code is None and "spawn failed" in err
    assert failing_assign and _gone(failing_assign[0])
    time.sleep(0.3)
    assert not marker.exists()


@windows_only
def test_verification_command_fails_closed(failing_assign, native_task):
    marker = native_task / "ran"
    result = asyncio.run(run_command([sys.executable, "-c", _marker_code(marker)], cwd=str(native_task),
                                    timeout=30, env=dict(os.environ), writable_cwd=True))
    assert not result.passed and "simulated AssignProcessToJobObject failure" in (result.error or "")
    assert failing_assign and _gone(failing_assign[0])
    time.sleep(0.3)
    assert not marker.exists()


@windows_only
def test_confine_spawned_raises_instead_of_resuming(failing_assign):
    with pytest.raises(OSError):
        worker_job.confine_spawned(12345678, suspended=True)


@windows_only
def test_app_refuses_to_start_without_a_job(monkeypatch, tmp_path: Path):
    def no_job() -> int:
        raise OSError(1450, "simulated CreateJobObjectW failure")

    monkeypatch.setattr(worker_job, "_ensure_job", no_job)
    app = create_app(data_dir=tmp_path / "data", providers={"mock": MockProvider()}, start_scheduler=False)
    with pytest.raises(RuntimeError, match="Job Object"):
        with TestClient(app):
            pass


@windows_only
def test_job_exists_once_the_app_started(tmp_path: Path):
    app = create_app(data_dir=tmp_path / "data", providers={"mock": MockProvider()}, start_scheduler=False)
    with TestClient(app):
        assert worker_job._job_handle is not None
        assert worker_job.active()
