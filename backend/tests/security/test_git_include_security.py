"""Included config is independently writable; never infer trust from its includer."""

import subprocess
import threading

import pytest

from orchestrator.gitops import git


@pytest.mark.parametrize("key", ["include.path", "includeIf.gitdir:*/.path"])
def test_filtering_refuses_includes_without_following_them(tmp_path, key):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    # A valid but untrusted include: even absent/inactive targets are refused.
    subprocess.run(["git", "-C", str(tmp_path), "config", key, str(tmp_path / "evil.conf")], check=True)
    with pytest.raises(git.UnsafeWorktree, match="configuration includes"):
        git.status_porcelain(tmp_path)


def test_included_filter_cannot_execute_at_medium(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    marker = tmp_path / "executed.txt"
    included = tmp_path / "agent-writable.conf"
    subprocess.run(["git", "config", "--file", str(included), "filter.evil.clean",
                    f"echo executed > '{marker.as_posix()}'; cat"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "include.path", str(included)], check=True)
    (tmp_path / ".gitattributes").write_text("* filter=evil\n")
    (tmp_path / "payload.txt").write_text("changed\n")
    with pytest.raises(git.UnsafeWorktree):
        git.commit_all(tmp_path, "must not execute included filter")
    assert not marker.exists()


def test_driver_scan_and_use_share_one_reservation(tmp_path, monkeypatch):
    target = git.GitTarget(str(tmp_path / ".git"), str(tmp_path), str(tmp_path), True)
    monkeypatch.setattr(git, "resolve", lambda path: target)
    monkeypatch.setattr(git, "BUSY_WAIT_SECONDS", 0.03)
    results = []

    def scan(path):
        def contender():
            try:
                with git.worktree_busy(tmp_path):
                    results.append("entered")
            except git.WorktreeBusy:
                results.append("blocked")
        th = threading.Thread(target=contender)
        th.start()
        th.join(2)
        assert not th.is_alive()
        return []

    monkeypatch.setattr(git, "_filter_overrides", scan)
    monkeypatch.setattr(git, "_run", lambda argv, *a, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    git.run_git(["status"], target)
    assert results == ["blocked"]
