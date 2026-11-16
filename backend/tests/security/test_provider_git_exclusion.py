"""A live provider cannot race the orchestrator's privileged Git operations."""

import pytest

from orchestrator.domain.states import TaskState
from orchestrator.gitops import git
from orchestrator.providers.mock import MockScript


async def test_git_waits_until_provider_has_finished(h):
    observed = []

    def scripter(request, index):
        assert git.is_busy(request.cwd)
        with pytest.raises(git.WorktreeBusy):
            git.run_git(["status"], request.cwd, busy_wait=0.01)
        observed.append(request.cwd)
        return MockScript.success(files={"result.txt": "ok"})

    h.mock._scripter = scripter
    project = await h.project()
    task = await h.task(project.id, verify_commands=[{"file_exists": "result.txt"}], autostart=True)
    assert await h.run_once(task.id) == TaskState.COMPLETED
    assert observed and all(not git.is_busy(path) for path in observed)


async def test_unverified_provider_cleanup_blocks_task_and_quarantines_git(h):
    from orchestrator.providers.base import AgentEvent, AgentEventKind, ExecutionResult

    async def unsafe_execute(request):
        try:
            yield AgentEvent(kind=AgentEventKind.RESULT, data=ExecutionResult(success=True).model_dump())
        finally:
            raise OSError("injected unverified provider cleanup")

    h.mock.execute = unsafe_execute
    project = await h.project()
    task = await h.task(project.id, verify_commands=[{"file_exists": "result.txt"}], autostart=True)
    assert await h.run_once(task.id) == TaskState.BLOCKED
    from orchestrator.db import tasks as tasks_repo

    current = await h.ctx.db.read(lambda tx: tasks_repo.get(tx, task.id))
    assert "cleanup could not be verified" in current.state_reason
    with pytest.raises(git.UnsafeWorktree, match="quarantined"):
        git.run_git(["status"], current.worktree_path)
    with pytest.raises(git.UnsafeWorktree, match="quarantined"):
        with git.worktree_busy(current.worktree_path):
            pytest.fail("quarantine must also exclude another provider or verifier")
