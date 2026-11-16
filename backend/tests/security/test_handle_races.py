"""Deterministic Windows directory-handle race regressions."""
import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.security import agent_files, paths, sandbox

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")


def swap(parent):
    with pytest.raises(OSError):
        parent.rename(parent.with_name(parent.name + "-moved"))


@pytest.mark.parametrize("operation", ["write", "mkdir", "remove", "restore"])
def test_parent_swap(tmp_path, monkeypatch, operation):
    root = tmp_path / "root"
    root.mkdir()
    before = agent_files.snapshot(str(root))
    parent = root / ".claude"
    parent.mkdir()
    (parent / "settings.json").write_bytes(b"inside")
    after = agent_files.snapshot(str(root))
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "settings.json"
    victim.write_bytes(b"survive")
    calls = []
    if operation == "write":
        original = paths._open_fd
        def hook(path, *args, **kwargs):
            swap(parent)
            calls.append(path)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(paths, "_open_fd", hook)
        paths.write_inside(root, ".claude/new", b"inside")
    elif operation == "mkdir":
        original = os.mkdir
        def hook(path, *args, **kwargs):
            if Path(path) == parent / "new":
                swap(parent)
                calls.append(path)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(os, "mkdir", hook)
        paths.make_dir_inside(root, ".claude/new")
    elif operation == "remove":
        original = os.scandir
        def hook(path):
            if Path(path) == parent:
                swap(parent)
                swap(root)
                calls.append(path)
            return original(path)
        monkeypatch.setattr(os, "scandir", hook)
        sandbox.remove_tree(parent)
    else:
        original = sandbox.remove_tree
        def hook(path):
            if Path(path).parent == parent:
                swap(parent)
                calls.append(path)
            return original(path)
        monkeypatch.setattr(sandbox, "remove_tree", hook)
        assert agent_files.restore(str(root), before, after) == []
    assert calls
    assert victim.read_bytes() == b"survive"
    assert not (outside / "new").exists()


def test_junction_target_survives(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_bytes(b"survive")
    link = root / "link"
    result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(paths.PathPolicyError):
            paths.write_inside(root, "link/new", b"bad")
        with pytest.raises(paths.PathPolicyError):
            paths.make_dir_inside(root, "link/newdir")
        sandbox.remove_tree(link)
        assert victim.read_bytes() == b"survive"
        assert not (outside / "new").exists()
        assert not (outside / "newdir").exists()
    finally:
        if link.exists():
            os.rmdir(link)


def test_held_empty_directory_cannot_become_junction(tmp_path):
    """FSCTL_SET_REPARSE_POINT requires a write handle, excluded by the guard."""
    import ctypes
    directory = tmp_path / "held"
    directory.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim").write_bytes(b"survive")
    with paths.directory_guard(directory):
        handle = paths._k32.CreateFileW(
            str(directory), paths._GENERIC_WRITE, paths._SHARE_ALL, None,
            paths._OPEN_EXISTING, paths._FLAG_BACKUP | paths._FLAG_OPEN_REPARSE, None)
        if handle and handle != paths._INVALID:
            paths._k32.CloseHandle(handle)
            pytest.fail("write handle could change the held directory's reparse data")
        assert ctypes.get_last_error() == 32  # ERROR_SHARING_VIOLATION
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(directory), str(outside)], capture_output=True, check=False)
        assert result.returncode != 0
        paths.write_inside(directory, "child", b"allowed")
        assert paths.read_inside(directory, "child", max_bytes=20) == b"allowed"
        assert [entry.name for entry in os.scandir(directory)] == ["child"]
        os.unlink(directory / "child")
    assert (outside / "victim").read_bytes() == b"survive"


def test_junction_swap_between_lstat_and_scan(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    parent.mkdir()
    child = parent / "child"
    child.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_bytes(b"survive")
    original = sandbox._lstat_reparse
    attacked = []
    def hook(path):
        st = original(path)
        if Path(path) == child and not attacked:
            try:
                child.rename(parent / "moved")
            except OSError as exc:
                assert exc.winerror == 32
                attacked.append(True)
                return st
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(child), str(outside)], capture_output=True, check=False)
            assert result.returncode == 0, result.stderr
            attacked.append(True)
        return st
    monkeypatch.setattr(sandbox, "_lstat_reparse", hook)
    try:
        try:
            sandbox.remove_tree(child)
        except paths.PathPolicyError:
            pass
        assert attacked
        assert victim.read_bytes() == b"survive"
    finally:
        if child.exists():
            os.rmdir(child)



def test_missing_tree_with_missing_parent_is_noop(tmp_path):
    sandbox.remove_tree(tmp_path / "missing" / "child")
