"""SECURITY_REVIEW #10: one server per data dir. A second instance fails fast and never runs recovery."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.api import app as app_mod
from orchestrator.api.app import create_app
from orchestrator.providers.mock import MockProvider
from orchestrator.security.instance_lock import LOCK_NAME, InstanceLock, InstanceLockError

BASE = "http://127.0.0.1:8765"

HOLDER = textwrap.dedent(
    """
    import sys, time
    from orchestrator.security.instance_lock import InstanceLock
    lock = InstanceLock(sys.argv[1]).acquire()
    print("LOCKED", flush=True)
    sys.stdin.readline()   # hold until the parent closes stdin / writes a line
    """
)


def _spawn_holder(data_dir: Path) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-c", HOLDER, str(data_dir)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "LOCKED"
    return p


def _stop(p: subprocess.Popen) -> None:
    try:
        p.stdin.write("\n")
        p.stdin.flush()
    except OSError:
        pass
    p.wait(timeout=20)


def test_second_acquisition_in_another_process_fails_fast(tmp_path):
    holder = _spawn_holder(tmp_path)
    try:
        with pytest.raises(InstanceLockError) as exc:
            InstanceLock(tmp_path).acquire()
        assert "already using this data directory" in str(exc.value) and "pid=" in str(exc.value)
    finally:
        _stop(holder)
    # holder gone (OS lock released with the process): acquisition works, the stale file does not block
    assert (tmp_path / LOCK_NAME).exists()
    with InstanceLock(tmp_path):
        pass


def test_killed_holder_leaves_stale_file_that_does_not_block(tmp_path):
    holder = _spawn_holder(tmp_path)
    holder.kill()
    holder.wait(timeout=20)
    assert (tmp_path / LOCK_NAME).exists()
    with InstanceLock(tmp_path) as lock:
        assert lock.held


def test_same_process_second_handle_is_refused_and_release_frees_it(tmp_path):
    a = InstanceLock(tmp_path).acquire()
    with pytest.raises(InstanceLockError):
        InstanceLock(tmp_path).acquire()
    a.release()
    a.release()  # idempotent
    InstanceLock(tmp_path).acquire().release()


def test_second_app_on_same_dir_fails_and_does_not_run_recovery(tmp_path, monkeypatch):
    calls = []
    real = app_mod.recover

    async def counting(ctx, agents):
        calls.append(1)
        return await real(ctx, agents)

    monkeypatch.setattr(app_mod, "recover", counting)
    data = tmp_path / "data"
    app1 = create_app(data_dir=data, providers={"mock": MockProvider()}, start_scheduler=False)
    with TestClient(app1, base_url=BASE):
        assert calls == [1]
        app2 = create_app(data_dir=data, providers={"mock": MockProvider()}, start_scheduler=False)
        with pytest.raises(InstanceLockError):
            with TestClient(app2, base_url=BASE):
                pass
        assert calls == [1]  # the loser never reached recovery
    # first instance shut down: the lock is free again and a new instance starts (recovery runs once more)
    app3 = create_app(data_dir=data, providers={"mock": MockProvider()}, start_scheduler=False)
    with TestClient(app3, base_url=BASE):
        assert calls == [1, 1]


def test_lock_held_by_foreign_process_blocks_app_startup(tmp_path, monkeypatch):
    calls = []
    real = app_mod.recover

    async def counting(ctx, agents):
        calls.append(1)
        return await real(ctx, agents)

    monkeypatch.setattr(app_mod, "recover", counting)
    holder = _spawn_holder(tmp_path / "data")
    try:
        app = create_app(data_dir=tmp_path / "data", providers={"mock": MockProvider()}, start_scheduler=False)
        with pytest.raises(InstanceLockError):
            with TestClient(app, base_url=BASE):
                pass
        assert calls == []
    finally:
        _stop(holder)
