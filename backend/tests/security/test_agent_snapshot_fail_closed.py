"""Unobservable or unrestorable agent configuration must refuse verification."""

import pytest

from orchestrator.security import agent_files


def test_oversized_protected_file_is_not_silently_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_files, "MAX_SNAPSHOT_BYTES", 8)
    (tmp_path / "AGENTS.md").write_bytes(b"a" * 9)
    with pytest.raises(OSError, match="cannot snapshot protected file"):
        agent_files.snapshot(str(tmp_path))


def test_total_snapshot_must_be_restorable(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_files, "MAX_SNAPSHOT_BYTES", 8)
    (tmp_path / "AGENTS.md").write_bytes(b"aaaaa")
    (tmp_path / "CLAUDE.md").write_bytes(b"bbbbb")
    with pytest.raises(OSError, match="restorable snapshot budget"):
        agent_files.snapshot(str(tmp_path))


def test_enumeration_failure_is_not_an_empty_snapshot(tmp_path, monkeypatch):
    def fail(*args):
        raise PermissionError("denied")

    monkeypatch.setattr(agent_files.os, "scandir", fail)
    with pytest.raises(OSError, match="cannot enumerate"):
        agent_files.snapshot(str(tmp_path))
