"""A failed driver scan must never permit an unprotected git invocation."""

import subprocess

import pytest

from orchestrator.gitops import git


@pytest.mark.parametrize("failure", ["oserror", "timeout", "exit"])
def test_failed_driver_scan_stops_git(monkeypatch, tmp_path, failure):
    target = git.GitTarget(str(tmp_path / ".git"), str(tmp_path), str(tmp_path))
    monkeypatch.setattr(git, "resolve", lambda path: target)
    calls = []

    def run(argv, cwd, **kwargs):
        calls.append(argv)
        if failure == "oserror":
            raise OSError("injected")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 30)
        return subprocess.CompletedProcess(argv, 128, "", "invalid config")

    monkeypatch.setattr(git, "_run", run)
    with pytest.raises(git.GitError):
        git.run_git(["status"], target)
    assert len(calls) == 1
    assert "config" in calls[0] and "status" not in calls[0]
