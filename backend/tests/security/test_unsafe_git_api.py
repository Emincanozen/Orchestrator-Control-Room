"""Read endpoints report unsafe filesystem entries without running further Git."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.api.routes import tasks
from orchestrator.gitops import git


@pytest.mark.parametrize("endpoint", ["diff", "artifacts"])
async def test_unsafe_worktree_has_explicit_read_response(monkeypatch, endpoint):
    runtime = SimpleNamespace(tasks=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(base_commit=None))))
    monkeypatch.setattr(tasks, "_worktree", AsyncMock(return_value="unused-fixture-path"))
    monkeypatch.setattr(git, "is_repo", lambda path: True)

    def refuse(*args, **kwargs):
        raise git.UnsafeWorktree("workspace filesystem link requires review")

    monkeypatch.setattr(git, "diff", refuse)
    monkeypatch.setattr(git, "changed_files", refuse)
    if endpoint == "diff":
        response = await tasks.task_diff("fixture-task", rt=runtime)
        assert response["available"] is False
    else:
        response = await tasks.task_artifacts("fixture-task", path=None, rt=runtime)
        assert response["files"] == []
    assert response["unsafe"] is True
    assert "requires review" in response["reason"]
