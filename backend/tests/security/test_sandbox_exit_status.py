"""Exit-status regressions: fake Win32 calls except for the explicit native test."""

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes as wt
from pathlib import Path

import pytest

from orchestrator.security import sandbox


class FakeWin32:
    def __init__(self):
        self.wait_result = 0  # WAIT_OBJECT_0
        self.exit_code = 259
        self.query_succeeds = True
        self.calls = []

    def WaitForSingleObject(self, handle, milliseconds):
        self.calls.append(("wait", handle, milliseconds))
        return self.wait_result

    def GetExitCodeProcess(self, handle, output):
        self.calls.append(("exit_code", handle))
        ctypes.cast(output, ctypes.POINTER(wt.DWORD)).contents.value = self.exit_code
        return self.query_succeeds

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        return True


@pytest.fixture
def fake_process(monkeypatch):
    api = FakeWin32()
    errors = []

    def winerr(operation):
        errors.append(operation)
        return OSError(5, f"{operation} failed: injected access denied")

    # Exercise the production class on every platform, without native handles or spawns.
    for name, value in {"_k32": api, "ctypes": ctypes, "wt": wt, "WAIT_OBJECT_0": 0,
                        "STILL_ACTIVE": 259, "_winerr": winerr}.items():
        monkeypatch.setattr(sandbox, name, value, raising=False)
    process = sandbox.SandboxedProcess(123456789, 123)
    try:
        yield process, api, errors
    finally:
        process.close()  # Must run before monkeypatch restores the real Win32 functions.


def test_imports_worker_source():
    expected = Path(__file__).resolve().parents[2] / "orchestrator" / "security" / "sandbox.py"
    assert Path(sandbox.__file__).resolve() == expected


@pytest.mark.parametrize("method", ["poll", "wait"])
@pytest.mark.parametrize("code, expected", [
    (0, 0), (42, 42), (259, 259), (0x80000000, -2147483648), (0xC0000005, -1073741819),
])
def test_signaled_exit_is_collected_and_cached(fake_process, method, code, expected):
    process, api, errors = fake_process
    api.exit_code = code

    assert getattr(process, method)() == expected
    assert process.returncode == expected
    assert process._h == 0
    assert api.calls == [("wait", 123, 0 if method == "poll" else 0xFFFFFFFF),
                         ("exit_code", 123), ("close", 123)]
    calls = list(api.calls)
    assert process.poll() == expected
    assert process.wait(timeout=0) == expected
    process.close()
    assert api.calls == calls
    assert errors == []


@pytest.mark.parametrize("method", ["poll", "wait"])
@pytest.mark.parametrize("output_on_failure", [0, 42, 259])
def test_exit_query_failure_raises_and_preserves_handle(fake_process, method, output_on_failure):
    process, api, errors = fake_process
    api.query_succeeds = False
    api.exit_code = output_on_failure

    with pytest.raises(OSError, match="GetExitCodeProcess failed") as caught:
        getattr(process, method)()

    assert caught.value.errno == 5
    assert errors == ["GetExitCodeProcess"]
    assert process.returncode is None
    assert process._h == 123
    assert api.calls == [("wait", 123, 0 if method == "poll" else 0xFFFFFFFF), ("exit_code", 123)]

    # An unsuccessful query must not discard the handle needed to retry collection/cleanup.
    api.query_succeeds = True
    api.exit_code = 259
    assert process.wait(timeout=0) == 259
    assert process._h == 0
    assert api.calls[-3:] == [("wait", 123, 0), ("exit_code", 123), ("close", 123)]


@pytest.mark.parametrize("method", ["poll", "wait"])
def test_nonsignaled_live_process_is_not_collected(fake_process, method):
    process, api, errors = fake_process
    api.wait_result = 0x102  # WAIT_TIMEOUT: exit code 259 must not imply completion.

    if method == "poll":
        assert process.poll() is None
    else:
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0)
    assert process.returncode is None
    assert process._h == 123
    assert api.calls == [("wait", 123, 0)]
    assert errors == []

    api.wait_result = 0
    assert process.poll() == 259
    assert process._h == 0


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows ExitProcess regression")
def test_native_exitprocess_259():
    """Run separately from heavy regression; no sandbox runtime/profile/ACL acquisition."""
    process = None
    child = subprocess.Popen([sys.executable, "-c", "import ctypes; ctypes.windll.kernel32.ExitProcess(259)"])
    try:
        # Own a separate query/synchronize handle; Popen retains ownership of its original handle.
        handle = sandbox._k32.OpenProcess(0x00100000 | sandbox.PROCESS_QUERY_LIMITED_INFORMATION, False, child.pid)
        if not handle:
            raise sandbox._winerr("OpenProcess")
        process = sandbox.SandboxedProcess(child.pid, handle)
        assert process.wait(timeout=10) == 259
        assert process.poll() == 259
        assert process.returncode == 259
        assert process._h == 0
        assert child.wait(timeout=5) == 259
    finally:
        if process is not None:
            process.close()
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows verification regression")
async def test_native_verification_reports_exit_259_without_false_timeout(tmp_path):
    """Production verification, real private runtime/AppContainer/job; no provider."""
    from orchestrator.gitops import git, worktrees
    from orchestrator.scheduler import verification
    from orchestrator.security import acl, appcontainer

    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("synthetic verification fixture\n")
    base = git.init_repo(source)
    task = Path(worktrees.create(source, tmp_path / "task", "exit-status", base).path)
    assert not any(a["sid"] == appcontainer.task_sid(task) for a in acl.read_security(task)["aces"] or [])
    result = await verification.run_command(
        [sys.executable, "-c", "import ctypes; ctypes.windll.kernel32.ExitProcess(259)"],
        str(task), 60, dict(os.environ), writable_cwd=True,
        sandbox_root=tmp_path / "scratch", tmp_dir=tmp_path / "logs",
    )
    assert not result.passed
    assert result.exit_code == 259, (result.error, result.output)
