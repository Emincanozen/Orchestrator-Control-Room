"""Git and verification must exclude each other in both start orders."""

import subprocess
import threading

import pytest

from orchestrator.gitops import git, worktrees


def test_verification_waits_for_git_already_running(tmp_path, monkeypatch):
    running, release, verified = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def fake_run(argv, **kwargs):
        running.set()
        assert release.wait(3)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(git.worker_job, "run_confined", fake_run)

    def invoke_git():
        try:
            git._run(["git", "status"], tmp_path, timeout=3)
        except Exception as exc:  # noqa: BLE001 - transfer thread failures to the test assertion
            errors.append(exc)

    def verify():
        try:
            with git.worktree_busy(tmp_path):
                verified.set()
        except Exception as exc:  # noqa: BLE001 - transfer thread failures to the test assertion
            errors.append(exc)

    worker = threading.Thread(target=invoke_git)
    verifier = threading.Thread(target=verify)
    worker.start()
    try:
        assert running.wait(2)
        verifier.start()
        assert not verified.wait(0.1)
    finally:
        release.set()
        worker.join(4)
        if verifier.ident:
            verifier.join(4)
    assert verified.is_set() and not errors


def test_raw_git_cannot_bypass_verification_reservation(tmp_path, monkeypatch):
    monkeypatch.setattr(git, "BUSY_WAIT_SECONDS", 0.02)
    called = []
    monkeypatch.setattr(git.worker_job, "run_confined", lambda *a, **kw: called.append(a))
    with git.worktree_busy(tmp_path), pytest.raises(git.WorktreeBusy):
        git._run(["git", "ls-files"], tmp_path, timeout=1)
    assert not called


def test_worktree_remove_checks_target_reservation(tmp_path, monkeypatch):
    monkeypatch.setattr(git, "BUSY_WAIT_SECONDS", 0.02)
    called = []
    monkeypatch.setattr(git, "run_git", lambda *a, **kw: called.append(a))
    with git.worktree_busy(tmp_path / "task"), pytest.raises(git.WorktreeBusy):
        worktrees.remove(tmp_path / "repo", tmp_path / "task")
    assert not called


def test_git_operations_on_one_tree_cannot_race_each_other(tmp_path):
    entered, finished = threading.Event(), threading.Event()

    def second_operation():
        entered.set()
        with git._git_operation(tmp_path):
            finished.set()

    with git._git_operation(tmp_path):
        # Nested plumbing on this thread must remain reentrant.
        with git._git_operation(tmp_path / "child"):
            pass
        thread = threading.Thread(target=second_operation)
        thread.start()
        assert entered.wait(2)
        assert not finished.wait(0.05)
    thread.join(2)
    assert finished.is_set() and not thread.is_alive()
