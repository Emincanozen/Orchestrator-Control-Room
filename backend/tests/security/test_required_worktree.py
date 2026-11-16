"""ER-04: pending/denied Git init must never permit provider edits in the source checkout."""
from pathlib import Path

import pytest

from orchestrator.db import projects as project_repo
from orchestrator.domain.states import TaskState
from orchestrator.providers.mock import MockScript
from orchestrator.scheduler.recovery import recover
from orchestrator.testing import make_harness


@pytest.mark.parametrize("mode", ["pending", "denied"])
async def test_no_provider_starts_without_isolated_worktree(h, mode):
    project = await h.project("original", git_repo=False,
                              files={"original.txt": "must remain unchanged"})
    if mode == "denied":
        approval, = await h.approvals.list("pending")
        await h.approvals.resolve(approval.id, False)
    h.mock.queue(MockScript.success("must not run", files={"original.txt": "overwritten"}))
    task = await h.task(project.id, autostart=True, verify_commands=[{"file_exists": "original.txt"}])
    assert await h.run_once(task.id) == TaskState.BLOCKED
    current = await h.tasks.get(task.id)
    assert "isolated Git worktree" in current.state_reason
    assert not h.mock.requests
    assert current.worktree_path is None
    assert (Path(project.path) / "original.txt").read_text() == "must remain unchanged"


@pytest.mark.parametrize("invalid", ["not_repo", "subdirectory"])
async def test_legacy_upgrade_refuses_unavailable_or_non_root_repository(h, invalid):
    project = await h.project("legacy-invalid", git_repo=invalid != "not_repo",
                              files={"nested/original.txt": "untouched"} if invalid == "subdirectory" else None)
    project_path = str(Path(project.path) / "nested") if invalid == "subdirectory" else project.path
    await h.ctx.db.write(lambda tx: tx.execute(
        "UPDATE projects SET path = ?, is_git = 1, isolation = 'in_place' WHERE id = ?",
        (project_path, project.id)))
    task = await h.task(project.id, autostart=True, verify_commands=[{"file_exists": "original.txt"}])
    assert await h.run_once(task.id) == TaskState.BLOCKED
    assert not h.mock.requests
    assert (await h.projects.get(project.id)).isolation == "in_place"


async def test_legacy_git_project_and_original_task_path_recover_to_owned_worktree(h):
    project = await h.project("legacy", files={"original.txt": "must remain unchanged"})
    await h.ctx.db.write(lambda tx: project_repo.update(tx, project.id, {"isolation": "in_place"}))
    h.mock.queue(MockScript.success("ok", files={"original.txt": "task changes only"}))
    task = await h.task(project.id, autostart=True, verify_commands=[{"file_exists": "original.txt", "contains": "task changes only"}])
    await h.tasks.set_fields(task.id, {"worktree_path": project.path})
    assert await h.run_once(task.id) == TaskState.COMPLETED
    assert (await h.projects.get(project.id)).isolation == "worktree"
    current = await h.tasks.get(task.id)
    assert Path(current.worktree_path).resolve() != Path(project.path).resolve()
    assert (Path(project.path) / "original.txt").read_text() == "must remain unchanged"


async def test_approved_git_init_then_explicit_retry_uses_worktree(h):
    project = await h.project("original", git_repo=False, files={"original.txt": "untouched"})
    approval, = await h.approvals.list("pending")
    h.mock.queue(MockScript.success("ok", files={"result.txt": "owned task output"}))
    task = await h.task(project.id, autostart=True, verify_commands=[{"file_exists": "result.txt"}])
    assert await h.run_once(task.id) == TaskState.BLOCKED
    await h.approvals.resolve(approval.id, True)
    assert (await h.tasks.get(task.id)).state == TaskState.BLOCKED
    await h.scheduler.retry_task(task.id)
    assert await h.run_once(task.id) == TaskState.COMPLETED
    current = await h.tasks.get(task.id)
    assert Path(current.worktree_path).resolve() != Path(project.path).resolve()
    assert (Path(current.worktree_path) / "result.txt").read_text() == "owned task output"
    assert not (Path(project.path) / "result.txt").exists()


async def test_restart_preserves_legacy_task_but_never_executes_in_original(tmp_path):
    first = await make_harness(tmp_path)
    try:
        project = await first.project("legacy-restart", files={"original.txt": "untouched"})
        await first.ctx.db.write(lambda tx: project_repo.update(tx, project.id, {"isolation": "in_place"}))
        task = await first.task(project.id, autostart=True, verify_commands=[{"file_exists": "result.txt"}])
        await first.tasks.set_fields(task.id, {"worktree_path": project.path})
    finally:
        await first.close()
    second = await make_harness(tmp_path)
    try:
        await recover(second.ctx, second.agents)
        second.mock.queue(MockScript.success("ok", files={"result.txt": "owned output"}))
        assert await second.run_once(task.id) == TaskState.COMPLETED
        current = await second.tasks.get(task.id)
        assert Path(current.worktree_path).resolve() != Path(project.path).resolve()
        assert not (Path(project.path) / "result.txt").exists()
        assert (Path(project.path) / "original.txt").read_text() == "untouched"
    finally:
        await second.close()
