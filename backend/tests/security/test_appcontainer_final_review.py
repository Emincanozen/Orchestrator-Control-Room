"""Bounded native checks for runtime leases and private child-created directories."""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import pytest

from orchestrator.scheduler import verification
from orchestrator.security import acl, appcontainer, python_runtime, sandbox

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows checks")


@pytest.fixture(autouse=True)
def verification_stage_evidence(caplog):
    # Keep original timing evidence on a future intermittent failure, not only a replay.
    caplog.set_level(logging.INFO, logger="orchestrator.scheduler.verification")


@pytest.fixture
def task(tmp_path):
    cwd = tmp_path / "worktree"
    cwd.mkdir()  # a plain directory: the sandbox identity is only ever granted on the per-command copy
    (cwd / ".git").write_text("gitdir: unused-by-this-test\n")
    return cwd


def run(task, argv):
    return asyncio.run(verification.run_command(
        argv, str(task), 15, dict(os.environ), writable_cwd=True,
        sandbox_root=task.parent / "sandbox", tmp_dir=task.parent / "logs"))


def test_child_0700_directory_has_protected_exact_identity_acl(task):
    code = ("import os,pathlib; os.mkdir('ordinary-dir',0o777); os.mkdir('private-dir',0o700); "
            "pathlib.Path('private-dir/own-file').write_text('ok'); "
            "print('PRIVATE-DIR-CREATED')")
    import tempfile

    # Through the session API, so the command's copy can be inspected before it is destroyed.
    session = sandbox.Session(str(task), task.parent / "sandbox", writable_cwd=True).prepare()
    copy = Path(session.cwd)
    fd, log_path = tempfile.mkstemp(dir=task.parent)
    proc = job = None
    try:
        proc, job = verification.sandbox_start(session, [sys.executable, "-c", code], dict(os.environ), fd)
        assert proc.wait(15) == 0, Path(log_path).read_text()
        job.close()
        descriptor = acl.read_security(copy / "private-dir")
        assert descriptor["protected"] is True
        allowed = {ace["sid"]: ace for ace in descriptor["aces"] if ace["type"] == 0}
        sid = appcontainer.task_sid(copy)
        assert set(allowed) == {acl.current_user_sid(), acl.SID_SYSTEM, sid}
        assert all(not ace["inherited"] for ace in allowed.values())
        assert allowed[sid]["mask"] == 0x1f01ff
        assert (copy / "private-dir/own-file").read_text() == "ok"
        assert acl.read_security(copy / "ordinary-dir")["protected"] is False
        assert session.finish() == []
    finally:
        if job is not None:
            job.close()
        if proc is not None:
            proc.close()
        session.finish()
        os.close(fd)
        Path(log_path).unlink()
    assert not copy.exists() and not (task / "private-dir").exists() and not (task / "ordinary-dir").exists()


def test_private_directory_creation_never_replaces_existing_destination(task):
    target = task / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("original")
    code = ("import os,pathlib; "
            "\ntry: os.mkdir('existing',0o700)"
            "\nexcept FileExistsError: pass"
            "\nelse: raise AssertionError('existing directory replaced')"
            "\nassert pathlib.Path('existing/keep.txt').read_text()=='original'"
            "\nassert not any(p.name.startswith('private-') for p in pathlib.Path(os.environ['TEMP']).iterdir())")
    result = run(task, [sys.executable, "-c", code])
    assert result.passed, result.error or result.output
    assert (target / "keep.txt").read_text() == "original"


def test_real_pytest_conftest_and_tmp_path_preserve_pass_and_failure(task):
    (task / "sibling.py").write_text("answer = 42\n")
    (task / "conftest.py").write_text(
        "import pytest,sibling\n@pytest.fixture\ndef answer(): return sibling.answer\n")
    test = task / "test_project.py"
    test.write_text("def test_answer(answer,tmp_path):\n"
                    "    (tmp_path/'own').write_text('ok')\n    assert answer == 42\n")
    result = run(task, [sys.executable, "-m", "pytest", "-q"])
    assert result.passed and "1 passed" in result.output, result.error or result.output
    test.write_text("def test_answer(answer,tmp_path):\n"
                    "    (tmp_path/'own').write_text('ok')\n    assert answer == 43\n")
    result = run(task, [sys.executable, "-m", "pytest", "-q"])
    assert not result.passed and result.exit_code == 1 and "1 failed" in result.output


def test_resolved_non_python_argv_does_not_acquire_python_runtime():
    class NoPythonLease:
        def acquire(self):
            raise AssertionError("non-Python must not acquire a Python runtime")
    argv = ["C:/Program Files/nodejs/node.exe", "-e", "console.log('ok')"]
    assert python_runtime.verification_argv(argv, cwd="unused", env={}, lease=NoPythonLease()) == argv


def test_native_private_npm_executes_and_preserves_script_failure(task):
    from orchestrator.security import node_runtime

    node = node_runtime.installed_node()
    npm = node.parent / "node_modules/npm/bin/npm-cli.js"
    (task / "package.json").write_text(json.dumps({"name": "native-appcontainer-check", "version": "1.0.0",
        "scripts": {"test": "node -e \"require('fs').writeFileSync('npm-ran','ok');console.log('NPM-SCRIPT-RAN')\"",
                    "fail": "node -e \"process.exit(7)\""}}))
    def execute(argv):
        return asyncio.run(verification.run_command(argv, str(task), 60, dict(os.environ), writable_cwd=True,
            sandbox_root=task.parent / "sandbox", tmp_dir=task.parent / "logs"))
    result = execute([str(node), str(npm), "test", "--offline"])
    assert result.passed, (result.error, result.exit_code, result.output)
    assert "NPM-SCRIPT-RAN" in result.output and not (task / "npm-ran").exists()  # written to the copy only
    result = execute([str(node.parent / "npm.cmd"), "run", "fail"])
    assert not result.passed and result.exit_code == 7, (result.error, result.output)


def test_bare_windows_tool_resolution_inside_python_appcontainer(task):
    code = ("import subprocess; "
            "log=open('native-child.log','w'); "
            "child=subprocess.run(['cmd.exe','/d','/c','echo','BARE-TOOL-OK'],"
            "stdout=log,stderr=log,timeout=5); assert child.returncode==0,child.returncode; "
            "log.close(); print(open('native-child.log').read())")
    result = run(task, [sys.executable, "-c", code])
    assert result.passed, result.error or result.output
    assert "BARE-TOOL-OK" in result.output and not (task / "native-child.log").exists()


def test_session_holds_runtime_lease_through_lazy_imports_and_job_completion(task):
    import ctypes
    import tempfile

    session = sandbox.Session(str(task), task.parent / "scratch", writable_cwd=True).prepare()
    fd, log_path = tempfile.mkstemp(dir=task.parent)
    proc = job = None
    exe = None
    try:
        proc, job = verification.sandbox_start(session,
            [sys.executable, "-c", "import ctypes,ssl; print('LAZY-IMPORTS-OK')"], dict(os.environ), fd)
        exe = session._runtime_lease.executable
        assert exe is not None
        proc.wait(10)
        job.close()
        # Handles remain held even after process exit, until Session.finish.
        handle = sandbox._k32.CreateFileW(str(exe), 0x40000000, 7, None, 3, 0, None)
        try:
            assert handle == sandbox.INVALID_HANDLE_VALUE
            assert ctypes.get_last_error() == 32
        finally:
            if handle and handle != sandbox.INVALID_HANDLE_VALUE:
                sandbox._k32.CloseHandle(handle)
        entries = [e for e in python_runtime._shared_runtimes.values() if e.core.executable == exe]
        assert len(entries) == 1, "the session's runtime must be the process-wide shared lease"
        entry = entries[0]
        refs_before = entry.refs
        assert refs_before >= 1
        assert session.finish() == []
        # Finishing drops exactly this session's reference; the pins (write/delete denial) stay until explicit
        # shutdown of unreferenced runtimes.
        assert entry.refs == refs_before - 1
        handle = sandbox._k32.CreateFileW(str(exe), 0x40000000, 7, None, 3, 0, None)
        try:
            assert handle == sandbox.INVALID_HANDLE_VALUE and ctypes.get_last_error() == 32
        finally:
            if handle and handle != sandbox.INVALID_HANDLE_VALUE:
                sandbox._k32.CloseHandle(handle)
        import gc
        gc.collect()  # abandoned sessions of earlier tests drop their references through weakref finalizers
        if entry.refs:
            # Another session in this test process still holds the shared runtime (for example one an earlier
            # test deliberately left retained after an unverified job exit). Its pins must then stay: the
            # release half of this test cannot apply, and shutdown must keep the entry.
            assert python_runtime.shutdown_shared_runtimes() >= 1
            assert "LAZY-IMPORTS-OK" in Path(log_path).read_text()
            return
        assert python_runtime.shutdown_shared_runtimes() == 0
        handle = sandbox._k32.CreateFileW(str(exe), 0x40000000, 7, None, 3, 0, None)
        released_open_error = ctypes.get_last_error()
        # Preserve the failure category without altering the access mask or deadline.
        import gc
        other_leases = [{"guards": len(obj._held._exit_callbacks), "file_pins": len(obj._pins),
                         "cleanup_failed": obj._cleanup_failed} for obj in gc.get_objects()
                        if type(obj) is python_runtime.RuntimeLease and obj.executable == exe]
        assert handle and handle != sandbox.INVALID_HANDLE_VALUE, (
            f"released interpreter write-open failed: winerror={released_open_error}; "
            f"own_callbacks={len(session._runtime_lease._held._exit_callbacks)}; "
            f"own_file_pins={len(session._runtime_lease._pins)}; "
            f"other_runtime_lease_callbacks={other_leases}; process_handle={bool(proc._h)}; "
            f"job_drained={job._drained}")
        sandbox._k32.CloseHandle(handle)  # access only; never modify the interpreter
        assert "LAZY-IMPORTS-OK" in Path(log_path).read_text()
    finally:
        if job is not None:
            job.close()
        if proc is not None:
            proc.wait(10)
            proc.close()
        session.finish()
        os.close(fd)
        Path(log_path).unlink()
