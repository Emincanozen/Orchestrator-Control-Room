"""Medium Git must not follow worktree junctions into other directories."""

import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.gitops import git

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows junctions")


@pytest.mark.parametrize("operation", ["diff", "reset", "commit"])
def test_git_never_reads_or_overwrites_a_junction_target(tmp_path, operation):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    payload = repo / "payload"
    payload.mkdir()
    (payload / "file.txt").write_text("base\n")
    base = git.commit_all(repo, "fixture base")
    victim = tmp_path / "outside"
    victim.mkdir()
    (victim / "file.txt").write_text("FAKE_PRIVATE_CONTENT\n")
    (payload / "file.txt").unlink()
    payload.rmdir()
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(payload), str(victim)],
                   capture_output=True, check=True)
    try:
        with pytest.raises(git.UnsafeWorktree, match="junction"):
            if operation == "diff":
                git.diff(repo, base)
            elif operation == "reset":
                git.reset_hard(repo, base)
            else:
                git.commit_all(repo, "must not stage an outside file")
        assert (victim / "file.txt").read_text() == "FAKE_PRIVATE_CONTENT\n"
    finally:
        if payload.is_junction():
            payload.rmdir()  # remove this fixture link only, never its target


def test_owned_worktree_cleanup_unlinks_junction_without_touching_target(tmp_path):
    from orchestrator.gitops import worktrees
    from security.test_n2_verification_sandbox import Env

    instance = Env(tmp_path)
    victim = tmp_path / "outside"
    victim.mkdir()
    (victim / "keep.txt").write_text("outside survives")
    link = instance.wt / "payload"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(victim)],
                   capture_output=True, check=True)
    try:
        worktrees.remove(instance.repo, instance.wt)
        assert not os.path.lexists(instance.wt)
        assert (victim / "keep.txt").read_text() == "outside survives"
        assert all(Path(row["worktree"]) != instance.wt for row in worktrees.list_worktrees(instance.repo))
    finally:
        instance.close()
