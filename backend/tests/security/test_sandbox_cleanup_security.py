"""Cleanup failures must not bypass integrity checks or leak Git reservations."""

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

from orchestrator.gitops import git
from orchestrator.scheduler import verification
from orchestrator.security import agent_files, sandbox, verify_tree


def test_cleanup_steps_are_independent_and_finish_is_idempotent(tmp_path, monkeypatch):
    session = sandbox.Session(str(tmp_path), tmp_path / "sandbox", writable_cwd=True)
    copy = Path(session.cwd)  # the session only ever inspects and restores its own copy
    copy.mkdir(parents=True)
    (copy / "AGENTS.md").write_text("original")
    session._agent_snapshot = agent_files.snapshot(str(copy))
    (copy / "AGENTS.md").write_text("malicious")
    session._gitlink = b"gitdir: original"
    session._tmp_created = True
    calls = []

    def fail(label):
        calls.append((label, session._finished))
        raise RuntimeError(label)

    class Desktop:
        def close(self):
            fail("desktop")

    session._desktop = Desktop()
    original_lstat = sandbox._lstat_reparse

    def lstat(path):
        if path == str(session.tmp.parent):
            return fail("temp")
        return original_lstat(path)

    monkeypatch.setattr(sandbox, "_lstat_reparse", lstat)
    monkeypatch.setattr(session, "_restore_gitlink", lambda: fail("gitlink"))
    # The hand-built copy was never registered by create(); keep the residue sweep away from it.
    monkeypatch.setattr(verify_tree, "sweep", lambda *a, **k: [])
    result = session.finish()
    # With a copy-in tree the scratch directory lives in the run directory and is removed with it (no
    # separate path-based temp removal that a read-only file could defeat).
    assert calls == [("desktop", False), ("gitlink", False)]
    assert (copy / "AGENTS.md").read_text() == "original"
    assert len(result) == 3
    assert session._finished
    assert session.finish() == result
    assert len(calls) == 2


def test_finish_exception_releases_busy_and_returns_security_failure(tmp_path, monkeypatch):
    def prepare(self):
        raise sandbox.SandboxUnavailable("injected preparation failure")

    def finish(self):
        assert git.is_busy(tmp_path)
        raise RuntimeError("injected finish failure")

    monkeypatch.setattr(sandbox.Session, "prepare", prepare)
    monkeypatch.setattr(sandbox.Session, "finish", finish)
    result = asyncio.run(verification.run_command(
        [sys.executable, "-c", "pass"], str(tmp_path), 10, dict(os.environ),
        sandbox_root=tmp_path / "sandbox", writable_cwd=True,
    ))
    assert not git.is_busy(tmp_path)
    assert not result.passed
    assert verification.is_security_error(result)
    assert result.error.startswith("sandbox_tamper:")
    assert "injected finish failure" in result.error


def test_unverified_command_job_exit_quarantines_tree(tmp_path, monkeypatch):
    class UndrainedJob:
        _drained = False

    def prepare(self):
        self._command_job = UndrainedJob()
        raise sandbox.SandboxUnavailable("injected unverified job exit")

    monkeypatch.setattr(sandbox.Session, "prepare", prepare)
    result = asyncio.run(verification.run_command(
        [sys.executable, "-c", "pass"], str(tmp_path), 10, dict(os.environ),
        sandbox_root=tmp_path / "sandbox", writable_cwd=True,
    ))
    assert not result.passed and verification.is_security_error(result)
    assert git.is_busy(tmp_path)
    with pytest.raises(git.UnsafeWorktree, match="quarantined"):
        with git._git_operation(tmp_path):
            pytest.fail("Git must remain excluded after unknown verification exit")


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows Low-integrity process")
def test_real_readonly_temp_does_not_hide_malicious_agent_file(tmp_path):
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    (cwd / ".git").write_bytes(b"gitdir: original\n")
    (cwd / "AGENTS.md").write_text("original")
    root = tmp_path / "sandbox"
    code = (
        "import os, pathlib, stat; "
        "p = pathlib.Path(os.environ['TEMP']) / 'readonly.txt'; "
        "p.write_text('hostile'); p.chmod(stat.S_IREAD); "
        "pathlib.Path('AGENTS.md').unlink(); pathlib.Path('AGENTS.md').write_text('malicious'); "
        "pathlib.Path('.git').unlink(); pathlib.Path('.git').write_text('gitdir: malicious\\n'); "
        "print('hostile command executed')"
    )
    try:
        result = asyncio.run(verification.run_command(
            [sys.executable, "-c", code], str(cwd), 15, dict(os.environ),
            sandbox_root=root, tmp_dir=tmp_path / "logs", writable_cwd=True,
        ))
        assert result.exit_code == 0, (result.output, result.error)
        assert "hostile command executed" in result.output
        assert not result.passed and verification.is_security_error(result)
        assert result.error.startswith("sandbox_tamper:")
        # MED-1: an ordinary read-only file is removed through its no-follow handle; cleanup is not tamper.
        assert "cannot remove" not in result.error
        assert list((root / "runs").iterdir()) == []
        assert "agent instruction/configuration files" in result.error
        assert ".git link" in result.error
        assert (cwd / "AGENTS.md").read_text() == "original"
        assert (cwd / ".git").read_bytes() == b"gitdir: original\n"
        assert not git.is_busy(cwd)
    finally:
        # Only the hostile file created in this test is made writable for pytest cleanup.
        for path in root.glob("runs/*/tmp/*/readonly.txt"):
            path.chmod(stat.S_IWRITE | stat.S_IREAD)
