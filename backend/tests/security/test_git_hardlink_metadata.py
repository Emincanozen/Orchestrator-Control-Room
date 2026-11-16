"""Defensive filesystem metadata checks only; no provider or sandbox execution."""

import os

import pytest

from orchestrator.gitops import git


def test_plain_worktree_refuses_real_hardlink_despite_cached_direntry_metadata(tmp_path):
    outside = tmp_path / "synthetic-outside.txt"
    outside.write_text("SYNTHETIC-CONTENT")
    tree = tmp_path / "task"
    tree.mkdir()
    os.link(outside, tree / "alias.txt")
    assert os.stat(tree / "alias.txt", follow_symlinks=False).st_nlink == 2
    with pytest.raises(git.UnsafeWorktree, match="hardlinked"):
        git.assert_plain_worktree(tree)
    assert outside.read_text() == "SYNTHETIC-CONTENT"


def test_plain_regular_worktree_remains_valid(tmp_path):
    (tmp_path / "ordinary.txt").write_text("synthetic")
    git.assert_plain_worktree(tmp_path)


def test_unreadable_fresh_metadata_refuses_worktree(tmp_path, monkeypatch):
    payload = tmp_path / "payload.txt"
    payload.write_text("synthetic")
    original = os.stat

    def denied(path, *args, **kwargs):
        if os.fspath(path) == str(payload):
            raise PermissionError("synthetic metadata denial")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", denied)
    with pytest.raises(git.UnsafeWorktree, match="cannot inspect"):
        git.assert_plain_worktree(tmp_path)


@pytest.mark.parametrize("metadata_directory", [False, True])
def test_provider_scan_includes_git_metadata_aliases(tmp_path, metadata_directory):
    outside = tmp_path / "outside.txt"
    outside.write_text("SYNTHETIC-METADATA")
    tree = tmp_path / "task"
    tree.mkdir()
    metadata = tree / ".git"
    if metadata_directory:
        metadata.mkdir()
        metadata /= "planted.txt"
    os.link(outside, metadata)
    # Medium Git resolves its own trusted metadata and intentionally ignores .git.
    git.assert_plain_worktree(tree)
    with pytest.raises(git.UnsafeWorktree):
        git.assert_plain_worktree(tree, include_git=True)
