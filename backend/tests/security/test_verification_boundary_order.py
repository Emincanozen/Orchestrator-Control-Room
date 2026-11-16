"""Synthetic preparation/cleanup ordering; no alias creation, child processes or ACL changes."""
import gc
import stat
import weakref
from types import SimpleNamespace

import pytest

from orchestrator.gitops import git
from orchestrator.security import agent_files, appcontainer, paths, sandbox, verify_tree


@pytest.fixture
def preparation(monkeypatch, tmp_path):
    cwd = tmp_path / "task"
    cwd.mkdir()
    (cwd / ".git").write_bytes(b"gitdir: synthetic\n")
    calls = []
    state = SimpleNamespace(calls=calls, reject_scan=False, reject_read=False)

    def where(root):
        # The real worktree is only ever scanned and read; grants/labels/pins target the disposable copy.
        if str(root) == str(cwd):
            return ""
        assert str(root) == state.session.cwd != str(cwd)
        return "copy-"

    def scan(root, *, check=None, max_entries=None):
        # The no-follow pre-scan (reparse points, multi-link files) is deadline/cancellation aware.
        assert check is not None
        calls.append(where(root) + "scan")
        if state.reject_scan:
            raise sandbox.SandboxTampered("synthetic unsupported filesystem object")

    def read(root, relative, *, max_bytes):
        assert relative == ".git" and max_bytes == 65536
        calls.append(where(root) + "git-read")
        if state.reject_read:
            raise paths.PathPolicyError("synthetic metadata identity changed")
        return b"gitdir: synthetic\n"

    monkeypatch.setattr(sandbox, "IS_WINDOWS", True)
    monkeypatch.setattr(sandbox.Session, "_check_data_dir", lambda self: None)
    monkeypatch.setattr(sandbox.Session, "_prepare_dirs", lambda self: calls.append("scratch"))
    monkeypatch.setattr(appcontainer, "task_sid", lambda path: "S-1-15-2-123")
    monkeypatch.setattr(appcontainer, "grant_directory",
                        lambda path, *args, **kwargs: calls.append(where(path) + "grant"))
    monkeypatch.setattr(sandbox, "label_low", lambda path, **kwargs: calls.append(where(path) + "label"))
    monkeypatch.setattr(sandbox, "pin_directory", lambda path: calls.append(where(path) + "pin"))
    monkeypatch.setattr(agent_files, "snapshot", lambda path: {})
    monkeypatch.setattr(verify_tree, "preflight", scan)
    monkeypatch.setattr(paths, "read_inside", read)
    state.session = sandbox.Session(str(cwd), tmp_path / "scratch", writable_cwd=True)
    return state


def test_unsupported_worktree_is_refused_before_permissions_or_scratch(preparation):
    preparation.reject_scan = True
    with pytest.raises(sandbox.SandboxTampered):
        preparation.session.prepare()
    assert preparation.calls == ["scan"]
    assert preparation.session._gitlink is None


def test_git_metadata_is_read_with_nofollow_bound_before_permissions(preparation):
    preparation.reject_read = True
    with pytest.raises(sandbox.SandboxTampered):
        preparation.session.prepare()
    assert preparation.calls == ["scan", "git-read"]


def test_plain_worktree_positive_order_and_snapshot(preparation):
    preparation.session.prepare()
    # Worktree: scanned and read only. Copy: granted while empty, filled, re-checked, then prepared as before.
    assert preparation.calls == ["scan", "git-read", "copy-grant", "copy-label", "copy-pin",
                                 "copy-scan", "copy-git-read", "scratch", "copy-grant", "copy-label", "copy-pin"]
    assert preparation.session._gitlink == b"gitdir: synthetic\n"
    assert (preparation.session._tree.path / ".git").read_bytes() == b"gitdir: synthetic\n"
    preparation.session.finish()
    assert not preparation.session._tree.base.exists()


def test_undrained_job_defers_all_cleanup_and_retains_owner(monkeypatch, tmp_path):
    calls = []
    job = SimpleNamespace(_drained=False)
    session = sandbox.Session(str(tmp_path), tmp_path / "scratch", writable_cwd=True)
    session._command_job = job
    session._desktop = SimpleNamespace(close=lambda: calls.append("desktop"))
    session._profile = SimpleNamespace(close=lambda: calls.append("profile"))
    session._runtime_lease = SimpleNamespace(close=lambda: calls.append("runtime"))
    session._tmp_created = True
    session._gitlink = b"synthetic"
    session._agent_snapshot = {}
    monkeypatch.setattr(session, "_restore_gitlink", lambda: calls.append("git-restore"))
    monkeypatch.setattr(sandbox, "remove_tree", lambda path: calls.append("remove"))
    monkeypatch.setattr(sandbox, "_lstat_reparse", lambda path: SimpleNamespace(
        st_file_attributes=0, st_mode=stat.S_IFDIR))
    monkeypatch.setattr(agent_files, "snapshot", lambda path: calls.append("agent-snapshot") or {})
    monkeypatch.setattr(agent_files, "diff", lambda *args: [])
    reference = weakref.ref(session)
    try:
        first = list(session.finish())
        assert first and calls == [] and not session._finished
        assert session.finish() == first  # no repeated findings or cleanup
        del session
        gc.collect()
        retained = reference()
        assert retained is not None, "uncertain job must retain runtime/guard ownership"
        job._drained = True  # simulated explicit barrier, not a native drain claim
        retained.finish()
        assert retained._finished and retained not in sandbox._RETAINED_SESSIONS
        # Scratch lives in the run directory and is removed with it (tree.destroy, after the runtime lease).
        assert calls == ["desktop", "profile", "git-restore", "agent-snapshot", "runtime"]
    finally:
        if reference() is not None:
            getattr(sandbox, "_RETAINED_SESSIONS", set()).discard(reference())
