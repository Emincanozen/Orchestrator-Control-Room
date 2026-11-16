"""Unknown Git worker exit must preserve a security outcome and block reuse."""

import subprocess

import pytest

from orchestrator.gitops import git, worktrees
from orchestrator.security import worker_job


def test_raw_git_drain_error_quarantines_before_reservation_release(tmp_path, monkeypatch):
    def unknown_exit(*args, **kwargs):
        raise worker_job.ContainmentError("injected unknown Git exit")

    monkeypatch.setattr(worker_job, "run_confined", unknown_exit)
    with pytest.raises(git.QuarantinedWorktree, match="Git worker exit unverified"):
        git._run(["git", "status"], tmp_path, timeout=1)
    assert git.is_busy(tmp_path)
    with pytest.raises(git.QuarantinedWorktree):
        with git.worktree_busy(tmp_path):
            pytest.fail("another writer must not enter")


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_worktree_mutation_quarantines_source_and_target(tmp_path, monkeypatch, operation):
    repo, target = tmp_path / "repo", tmp_path / "task"
    repo.mkdir()
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git.register_repo(repo)

    def unknown_exit(*args, **kwargs):
        raise worker_job.ContainmentError("injected mutation exit failure")

    monkeypatch.setattr(git, "_filter_overrides", lambda path: [])
    monkeypatch.setattr(worker_job, "run_confined", unknown_exit)
    args = (["worktree", "add", "--quiet", "-b", "branch", str(target), "HEAD"]
            if operation == "add" else ["worktree", "remove", "--force", str(target)])
    with pytest.raises(git.QuarantinedWorktree):
        git.run_git(args, repo)
    for path in (repo, target):
        assert git.is_busy(path)
        with pytest.raises(git.QuarantinedWorktree):
            with git.worktree_busy(path):
                pytest.fail("quarantined source/target must not be reusable")
    with pytest.raises(git.QuarantinedWorktree):
        worktrees.remove(repo, target)
    assert target.is_dir(), "unknown live writers forbid automatic removal"
