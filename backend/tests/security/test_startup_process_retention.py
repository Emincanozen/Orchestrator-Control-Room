"""A drained empty job is not proof that an unassigned startup process exited."""
import subprocess
from types import SimpleNamespace

import pytest

from orchestrator.security import appcontainer, node_runtime, python_runtime, sandbox


@pytest.fixture
def startup(monkeypatch, tmp_path):
    state = SimpleNamespace(calls=[], wait_fails=True)

    class Process:
        pid = 123456789  # Only a fake identifier, never sent to an OS function.

        def kill(self):
            state.calls.append("kill")

        def wait(self, timeout):
            state.calls.append("wait")
            if state.wait_fails:
                raise subprocess.TimeoutExpired("synthetic startup", timeout)
            return -1

        def close(self):
            state.calls.append("process-close")

    class Job:
        _drained = False

        def assign(self, pid):
            raise OSError("synthetic assignment refusal")

        def close(self):
            self._drained = True  # Empty: the process was never assigned.

    state.process = Process()
    monkeypatch.setattr(sandbox, "CommandJob", Job)
    monkeypatch.setattr(sandbox.Session, "env", lambda self, env: {})
    monkeypatch.setattr(node_runtime, "command_kind", lambda argv: None)
    monkeypatch.setattr(python_runtime, "RuntimeLease", lambda: SimpleNamespace(
        close=lambda: state.calls.append("runtime-close")))
    monkeypatch.setattr(python_runtime, "verification_argv", lambda argv, **kwargs: argv)
    monkeypatch.setattr(appcontainer, "Profile", lambda cwd: SimpleNamespace(
        sid="S-1-15-2-123", create=lambda: None, close=lambda: state.calls.append("profile-close")))
    monkeypatch.setattr(sandbox, "SandboxDesktop", lambda sid: SimpleNamespace(
        name="synthetic", close=lambda: state.calls.append("desktop-close")))
    monkeypatch.setattr(sandbox, "spawn", lambda *args, **kwargs: state.process)
    state.session = sandbox.Session(str(tmp_path), tmp_path / "scratch", writable_cwd=True)
    yield state
    sandbox._RETAINED_SESSIONS.discard(state.session)  # Fake-only teardown, not production recovery.


def test_unassigned_wait_timeout_retains_process_and_every_lease(startup):
    with pytest.raises(subprocess.TimeoutExpired):
        startup.session.start(["python.exe"], env={}, stdout_fd=-1, confine=lambda pid: None)
    assert startup.session._command_job._drained
    findings = startup.session.finish()
    assert findings and not startup.session._finished
    assert startup.calls == ["kill", "wait"]
    assert startup.session in sandbox._RETAINED_SESSIONS


def test_failed_startup_with_confirmed_exit_can_release(startup):
    startup.wait_fails = False
    with pytest.raises(sandbox.SandboxUnavailable):
        startup.session.start(["python.exe"], env={}, stdout_fd=-1, confine=lambda pid: None)
    assert startup.session.finish() == []
    assert startup.calls == ["kill", "wait", "process-close", "desktop-close", "profile-close", "runtime-close"]
    assert startup.session._finished
