"""Pure-unit native lifetime regressions: no processes, runtimes or ACL changes."""

import asyncio
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from orchestrator.gitops import git
from orchestrator.scheduler import verification
from orchestrator.security import appcontainer, node_runtime, python_runtime, sandbox


class Gate:
    """Hold a fake native operation; a watchdog also bounds failures on the old code."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.expired = False

    def wait(self):
        self.entered.set()
        if not self.release.wait(1):
            self.expired = True


async def entered(gate):
    deadline = time.monotonic() + 3
    while not gate.entered.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0)
    assert gate.entered.is_set(), "fake native operation was never reached"


async def turns():
    for _ in range(12):
        await asyncio.sleep(0)


@pytest.fixture
def native(tmp_path, monkeypatch):
    h = SimpleNamespace(trace=[], active=set(), gates={}, session=None, live=False,
                        resumed=False, returncode=None, unknown_exit=False, clock=0.0)
    h.cwd = str(tmp_path)

    def step(label):
        h.trace.append(label + ".begin")
        h.active.add(label)
        try:
            if label in h.gates:
                h.gates[label].wait()
        finally:
            h.active.remove(label)
            h.trace.append(label + ".end")

    class Process:
        pid = 123456789  # Never passed to an actual OS operation.

        @property
        def returncode(self):
            return h.returncode

        def poll(self):
            return h.returncode

        def wait(self, timeout):
            step("wait")
            h.returncode = 0 if h.returncode is None else h.returncode
            return h.returncode

        def kill(self):
            step("kill")
            h.returncode = -1

        def close(self):
            step("process_close")

    h.proc = Process()

    class Job:
        _drained = False

        def assign(self, pid):
            step("assign")

        def terminate(self):
            step("terminate")
            h.returncode = -1

        def close(self):
            step("drain")
            if h.unknown_exit:
                raise sandbox.SandboxUnavailable("fake unknown job exit")
            h.returncode = -1 if h.returncode is None else h.returncode
            self._drained = True

    class Profile:
        sid = "fake-task-sid"

        def __init__(self, cwd):
            pass

        def create(self):
            step("profile")

        def close(self):
            assert h.session._command_job._drained
            step("profile_close")

    class Desktop:
        name = "fake-desktop"

        def __init__(self, sid):
            step("desktop")

        def close(self):
            step("desktop_close")

    class Lease:
        def close(self):
            assert h.session._command_job._drained
            step("lease_close")

    def prepare(session):
        h.session = session
        step("prepare")
        return session

    original_finish = sandbox.Session.finish

    def finish(session):
        if session._finished:
            return session.tampered
        h.trace.append("finish_active:" + ",".join(sorted(h.active)))
        assert git.is_busy(h.cwd), "reservation released before finish"
        step("finish")
        return original_finish(session)

    def runtime(argv, **kwargs):
        step("runtime")
        return argv

    def spawn(*args, **kwargs):
        step("spawn")
        return h.proc

    def resume(pid):
        step("resume")
        h.resumed = True
        if not h.live:
            h.returncode = 0
        return True

    def kill_tree(pid):
        step("kill_tree")
        h.returncode = -1

    def forbidden(*args, **kwargs):
        pytest.fail("pure-unit test attempted a real subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(sandbox.Session, "prepare", prepare)
    monkeypatch.setattr(sandbox.Session, "finish", finish)
    monkeypatch.setattr(sandbox, "CommandJob", Job)
    monkeypatch.setattr(sandbox, "SandboxDesktop", Desktop)
    monkeypatch.setattr(sandbox, "spawn", spawn)
    monkeypatch.setattr(sandbox, "_resume", resume)
    monkeypatch.setattr(sandbox, "process_integrity_rid", lambda pid: step("integrity") or sandbox.LOW_RID)
    monkeypatch.setattr(appcontainer, "Profile", Profile)
    monkeypatch.setattr(appcontainer, "verify_process", lambda *args: step("verify"))
    monkeypatch.setattr(python_runtime, "RuntimeLease", Lease)
    monkeypatch.setattr(python_runtime, "verification_argv", runtime)
    monkeypatch.setattr(node_runtime, "NodeRuntimeLease", Lease)
    monkeypatch.setattr(node_runtime, "command_kind", lambda argv: "node" if argv[0] == "node" else None)
    monkeypatch.setattr(node_runtime, "verification_command", lambda argv, **kw: (runtime(argv, **kw), kw["env"]))
    monkeypatch.setattr(verification.worker_job, "confine_spawned", lambda *a, **kw: step("confine"))
    monkeypatch.setattr(verification, "resolve_executable", lambda exe, *a: exe)
    monkeypatch.setattr(verification, "_track_descendants", lambda *a: step("track"))
    monkeypatch.setattr(verification, "_kill_survivors", lambda *a: step("survivors"))
    monkeypatch.setattr(verification, "kill_tree", kill_tree)
    monkeypatch.setattr(verification, "_tail_text", lambda *a: "")
    monkeypatch.setattr(verification, "time", SimpleNamespace(monotonic=lambda: h.clock))
    fake_fd = 123456789
    monkeypatch.setattr(verification.tempfile, "mkstemp", lambda **kw: (fake_fd, str(tmp_path / "fake.log")))
    close = verification.os.close
    monkeypatch.setattr(verification.os, "close", lambda fd: step("fd_close") if fd == fake_fd else close(fd))
    unlink = verification.os.unlink
    monkeypatch.setattr(verification.os, "unlink",
                        lambda path, **kw: step("unlink") if str(path) == str(tmp_path / "fake.log") else unlink(path, **kw))

    def command(argv=None, timeout=10):
        return asyncio.create_task(verification.run_command(
            argv or ["python"], h.cwd, timeout, {}, sandbox_root=tmp_path / "sandbox", writable_cwd=True,
        ))

    h.command = command
    yield h
    for gate in h.gates.values():
        gate.release.set()
    if h.session is not None:
        sandbox._RETAINED_SESSIONS.discard(h.session)  # Fake-only owner teardown.


@pytest.mark.parametrize("exe", ["python", "node"])
async def test_event_loop_heartbeat_during_native_start(native, exe):
    gate = native.gates["runtime"] = Gate()
    task = native.command([exe])
    try:
        await entered(gate)
        await turns()  # The loop services other coroutines while trusted native setup is pending.
        responsive = not gate.expired and not task.done()
    finally:
        gate.release.set()
        result = await task
    assert responsive, "native startup blocked the event loop"
    assert result.passed
    assert native.trace.index("confine.end") < native.trace.index("assign.end")
    assert native.trace.index("assign.end") < native.trace.index("verify.end")
    assert native.trace.index("verify.end") < native.trace.index("integrity.end")
    assert native.trace.index("integrity.end") < native.trace.index("resume.begin")


@pytest.mark.parametrize("phase", ["prepare", "runtime", "integrity"])
async def test_cancel_pending_setup_settles_before_finish_and_never_resumes(native, phase):
    gate = native.gates[phase] = Gate()
    task = native.command()
    try:
        await entered(gate)
        task.cancel()
        await turns()
        held = not task.done() and git.is_busy(native.cwd) and "finish.begin" not in native.trace
        task.cancel()  # A second cancellation cannot skip joining the native operation.
        await turns()
        held_again = not task.done() and "fd_close.begin" not in native.trace
    finally:
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert held and held_again
    assert not gate.expired
    assert not native.resumed, "pending native launch resumed after cancellation"
    assert native.trace.index(phase + ".end") < native.trace.index("finish.begin")
    assert "finish_active:" in native.trace
    assert not git.is_busy(native.cwd)
    if phase == "prepare":
        assert "runtime.begin" not in native.trace
    if phase == "integrity":
        assert native.trace.index("kill.end") < native.trace.index("process_close.begin")


async def test_cancel_late_start_result_retains_handles_until_drain(native, monkeypatch):
    gate = Gate()
    drain = native.gates["drain"] = Gate()
    original = verification.sandbox_start

    def late_start(*args):
        result = original(*args)
        gate.wait()  # Resume already happened; ownership has not yet returned to the caller.
        native.trace.append("start_return")
        return result

    monkeypatch.setattr(verification, "sandbox_start", late_start)
    native.live = True
    task = native.command()
    try:
        await entered(gate)
        task.cancel()
        await turns()
        held = not task.done() and "fd_close.begin" not in native.trace and git.is_busy(native.cwd)
        task.cancel()
        await turns()
        gate.release.set()
        await entered(drain)
        task.cancel()  # Cancellation also cannot interrupt cleanup itself.
        await turns()
        cleanup_held = not task.done() and git.is_busy(native.cwd) and "finish.begin" not in native.trace
        task.cancel()
        await turns()
        cleanup_held_again = not task.done() and "process_close.begin" not in native.trace
    finally:
        gate.release.set()
        drain.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert held and cleanup_held and cleanup_held_again
    assert not gate.expired and not drain.expired
    assert native.trace.index("start_return") < native.trace.index("fd_close.begin")
    assert native.trace.index("kill_tree.end") < native.trace.index("drain.begin")
    assert native.trace.index("drain.end") < native.trace.index("process_close.begin")
    assert native.trace.index("process_close.end") < native.trace.index("finish.begin")
    assert native.trace.index("drain.end") < native.trace.index("lease_close.begin")
    assert not git.is_busy(native.cwd)


@pytest.mark.parametrize("phase", ["track", "wait", "drain", "finish"])
async def test_cancel_each_background_operation_settles_despite_repeated_cancel(native, phase):
    gate = native.gates[phase] = Gate()
    native.live = phase == "track"
    task = native.command()
    try:
        await entered(gate)
        task.cancel()
        await turns()
        held = not task.done() and git.is_busy(native.cwd)
        task.cancel()
        await turns()
        held_again = not task.done() and git.is_busy(native.cwd)
    finally:
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert held and held_again and not gate.expired
    assert not any(item.startswith("finish_active:") and item != "finish_active:" for item in native.trace)
    assert native.trace.index(phase + ".end") < native.trace.index("unlink.begin")
    if phase == "finish":
        assert native.trace.count("finish.begin") == 1
    assert not git.is_busy(native.cwd)


async def test_native_setup_does_not_restart_command_budget(native, monkeypatch):
    original = python_runtime.verification_argv

    def slow_runtime(*args, **kw):
        native.clock = 12.0  # Original 10-second budget is already exhausted at startup completion.
        return original(*args, **kw)

    monkeypatch.setattr(python_runtime, "verification_argv", slow_runtime)
    native.live = True
    result = await native.command(timeout=10)
    assert result.timed_out and not result.passed
    assert result.error == "timed out after 10s"
    assert not native.resumed
    assert "spawn.begin" not in native.trace
    assert not git.is_busy(native.cwd)


async def test_budget_expired_during_confinement_never_resumes(native, monkeypatch):
    def late_verify(*args):
        native.clock = 12.0

    monkeypatch.setattr(appcontainer, "verify_process", late_verify)
    result = await native.command(timeout=10)
    assert result.timed_out and not result.passed
    assert not native.resumed
    assert "kill.end" in native.trace  # The suspended process is drained before lease release.
    assert not git.is_busy(native.cwd)


async def test_budget_expired_during_prepare_never_starts(native, monkeypatch):
    original = sandbox.Session.prepare

    def late_prepare(session):
        result = original(session)
        native.clock = 12.0
        return result

    monkeypatch.setattr(sandbox.Session, "prepare", late_prepare)
    result = await native.command(timeout=10)
    assert result.timed_out and not result.passed
    assert not native.resumed
    assert "runtime.begin" not in native.trace
    assert "spawn.begin" not in native.trace
    assert not git.is_busy(native.cwd)


async def test_exit_observed_after_deadline_is_not_pass(native, monkeypatch):
    def late_poll():
        native.clock = 12.0
        return 0

    monkeypatch.setattr(native.proc, "poll", late_poll)
    result = await native.command(timeout=10)
    assert native.resumed
    assert result.timed_out and not result.passed
    assert not git.is_busy(native.cwd)


async def test_unknown_job_exit_keeps_worktree_quarantined(native):
    native.unknown_exit = True
    result = await native.command()
    assert not result.passed and verification.is_security_error(result)
    assert git.is_busy(native.cwd)
    assert not native.session._command_job._drained
    assert "lease_close.begin" not in native.trace
    assert "profile_close.begin" not in native.trace


async def test_unknown_startup_exit_quarantines_even_when_command_job_is_empty(native, monkeypatch):
    def refused_start(session, *args, **kwargs):
        session._command_job = SimpleNamespace(_drained=True)
        session._unverified_startup_process = native.proc
        raise sandbox.SandboxUnavailable("synthetic unverified startup process")

    monkeypatch.setattr(verification, "sandbox_start", refused_start)
    result = await native.command()
    assert not result.passed and verification.is_security_error(result)
    assert git.is_busy(native.cwd), "empty-job drain must not clear startup-process uncertainty"


async def test_cancel_during_resume_keeps_event_loop_responsive_and_joins_late_process(native):
    gate = native.gates["resume"] = Gate()
    native.live = True
    task = native.command()
    try:
        await entered(gate)
        task.cancel()
        await turns()
        responsive = not gate.expired and not task.done() and git.is_busy(native.cwd)
        task.cancel()
        await turns()
    finally:
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert responsive, "cancellation blocked the event loop on the native resume lock"
    assert native.resumed
    assert native.trace.index("resume.end") < native.trace.index("kill_tree.begin")
    assert native.trace.index("drain.end") < native.trace.index("lease_close.begin")
    assert not git.is_busy(native.cwd)


@pytest.mark.parametrize("phase", ["runtime", "integrity"])
async def test_event_loop_teardown_cannot_end_the_native_join(native, phase):
    """asyncio.run / Runner.close cancels EVERY task of the loop directly (server shutdown, test loop teardown).
    That must not end the join of a running native startup: cleanup would otherwise see an undrained job,
    defer finish and retain the session (shared runtime reference, run directory) for the process lifetime."""
    gate = native.gates[phase] = Gate()
    task = native.command()
    try:
        await entered(gate)
        for _ in range(2):
            for other in asyncio.all_tasks():  # what asyncio.runners._cancel_all_tasks does
                if other is not asyncio.current_task():
                    other.cancel()
            await turns()
        held = not task.done() and git.is_busy(native.cwd) and "finish.begin" not in native.trace
    finally:
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert held, "loop teardown ended the native join early"
    assert not gate.expired and not native.resumed
    assert native.trace.index(phase + ".end") < native.trace.index("finish.begin")
    assert native.session._finished and native.session not in sandbox._RETAINED_SESSIONS
    assert "lease_close.end" in native.trace and not git.is_busy(native.cwd)


async def test_retained_session_is_settled_once_job_exit_becomes_verifiable(native):
    native.unknown_exit = True
    result = await native.command()
    session = native.session
    assert not result.passed and verification.is_security_error(result)
    assert session in sandbox._RETAINED_SESSIONS and not session._finished
    # Still unverifiable: nothing is released, the session stays retained (and the worktree quarantined).
    assert sandbox.settle_retained_sessions() >= 1
    assert session in sandbox._RETAINED_SESSIONS and not session._finished
    assert not session._command_job._drained
    assert "lease_close.begin" not in native.trace and "profile_close.begin" not in native.trace
    native.unknown_exit = False  # the job can now be terminated and reports no active process
    sandbox.settle_retained_sessions()
    assert session._finished and session not in sandbox._RETAINED_SESSIONS
    assert session._command_job._drained and "lease_close.end" in native.trace and "profile_close.end" in native.trace
    assert git.is_busy(native.cwd)  # the quarantine recorded for the unverified exit is not lifted
