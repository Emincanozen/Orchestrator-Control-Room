"""The executor never abandons a running verification: cancellation of the task run (scheduler stop after the
grace period, server shutdown, loop teardown) is propagated to the verification and then joined, so its native
cleanup (job drain, session finish, runtime release) completes before the run ends."""

import asyncio
import gc
import os
import threading
from types import SimpleNamespace

import pytest

from orchestrator.domain.states import TaskState as S
from orchestrator.scheduler.executor import StopRequested
from orchestrator.security import python_runtime, sandbox


def slow_verifier(state):
    async def verify(task, cwd, *, specs, origin):
        state.started.set()
        try:
            while True:  # like verification._native: joins trusted native work through repeated cancellation
                try:
                    await asyncio.shield(state.release.wait())
                    break
                except asyncio.CancelledError:
                    state.cancels += 1
                    if state.release.is_set():
                        break
        finally:
            state.finished = True
        raise asyncio.CancelledError()

    return verify


@pytest.fixture
def slow(h, monkeypatch):
    state = SimpleNamespace(started=asyncio.Event(), release=asyncio.Event(), cancels=0, finished=False)
    monkeypatch.setattr(h.executor.verifier, "verify", slow_verifier(state))
    state.task = SimpleNamespace(id="tsk_x", project_id="prj_x", verify_commands=[["python", "-c", "1"]], plan=None)
    state.project = SimpleNamespace(verify_commands=[])
    yield state
    state.release.set()  # never leave the fake verification pending, also when an assertion failed


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


async def test_cancelled_run_joins_its_verification(h, slow):
    run = asyncio.create_task(h.executor._verify(slow.task, slow.project, "cwd", None))
    await slow.started.wait()
    run.cancel()  # no stop request: plain task cancellation (scheduler.stop after grace, loop teardown)
    await settle()
    assert slow.cancels >= 1, "the verification was not cancelled with its run"
    assert not run.done() and not slow.finished, "the run ended while its verification was still cleaning up"
    run.cancel()  # repeated cancellation still cannot orphan it
    await settle()
    assert not run.done() and not slow.finished
    slow.release.set()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert slow.finished


async def test_stop_request_then_cancellation_joins_the_verification(h, slow):
    run = asyncio.create_task(h.executor._verify(slow.task, slow.project, "cwd", None))
    await slow.started.wait()
    await h.executor.request_stop("tsk_x", "shutdown")
    await settle()
    assert slow.cancels == 1 and not run.done()
    run.cancel()  # scheduler.stop: grace period over
    await settle()
    assert not run.done() and not slow.finished, "the run ended while its verification was still cleaning up"
    slow.release.set()
    with pytest.raises((asyncio.CancelledError, StopRequested)):
        await run
    assert slow.finished


@pytest.mark.skipif(os.name != "nt", reason="the verification sandbox is Windows only")
async def test_shutdown_during_real_verification_startup_releases_the_session(h, monkeypatch):
    """Real sandbox and managed runtime: the run is cancelled (scheduler.stop with no grace) and the loop then
    cancels every task (asyncio.run teardown) while native startup is running. The session must still finish:
    nothing retained, the shared runtime reference released, the per-command run directory gone."""
    in_start = threading.Event()
    sessions = []
    original = sandbox.Session.start

    def start(session, *args, **kwargs):
        sessions.append(session)
        in_start.set()
        return original(session, *args, **kwargs)

    monkeypatch.setattr(sandbox.Session, "start", start)
    project = await h.project()
    task = await h.task(project.id, acceptance_criteria=["c"], plan={"x": 1},
                        verify_commands=[["python", "-c", "print(1)"]])
    await h.tasks.transition(task.id, S.QUEUED, reason="s")
    assert await h.scheduler.tick() == [task.id]
    runs = list(h.scheduler._running.values())
    assert await asyncio.to_thread(in_start.wait, 60), "verification never started"
    await h.scheduler.stop(grace_seconds=0)
    for other in asyncio.all_tasks():  # what asyncio.runners._cancel_all_tasks does at loop teardown
        if other is not asyncio.current_task():
            other.cancel()
    leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    await asyncio.wait(runs + leftovers, timeout=120)
    assert all(t.done() for t in runs + leftovers)
    [session] = sessions
    assert session._finished and session not in sandbox._RETAINED_SESSIONS
    assert session.tampered == [] and not session.root.exists()
    assert session._runtime_lease is None or session._runtime_lease._entry is None
    gc.collect()
    assert all(entry.refs == 0 for entry in python_runtime._shared_runtimes.values())
