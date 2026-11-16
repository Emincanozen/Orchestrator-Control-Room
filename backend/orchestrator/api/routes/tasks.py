"""Task routes, controls and sub-resources."""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from orchestrator.api.deps import AUTH, get_rt
from orchestrator.api.runtime import Runtime
from orchestrator.db import records
from orchestrator.db import runs as runs_repo
from orchestrator.db import tasks as tasks_repo
from orchestrator.domain.events import Event
from orchestrator.domain.models import Checkpoint, Run, StateTransition, Task, TaskCreate, TaskUpdate, Verification
from orchestrator.domain.states import TaskState
from orchestrator.errors import Conflict, NotFound, ValidationFailed
from orchestrator.gitops import git
from orchestrator.security import paths as path_policy
from orchestrator.security.paths import resolve_inside
from orchestrator.security.redact import redact

router = APIRouter(prefix="/api/tasks", tags=["tasks"], dependencies=AUTH)

MAX_ARTIFACT_BYTES = 1_000_000


def _parse_states(state: Optional[str]) -> Optional[list[str]]:
    if not state:
        return None
    out = [s.strip().upper() for s in state.split(",") if s.strip()]
    valid = {s.value for s in TaskState}
    bad = [s for s in out if s not in valid]
    if bad:
        raise ValidationFailed(f"unknown state(s): {bad}")
    return out


@router.get("")
async def list_tasks(
    project_id: Optional[str] = None, state: Optional[str] = None, rt: Runtime = Depends(get_rt)
) -> list[Task]:
    return await rt.tasks.list(project_id=project_id, states=_parse_states(state))


@router.post("", status_code=201)
async def create_task(body: TaskCreate, rt: Runtime = Depends(get_rt)) -> Task:
    task = await rt.tasks.create(body)
    if body.autostart:
        rt.scheduler.wake()
    return task


@router.get("/{task_id}")
async def get_task(task_id: str, rt: Runtime = Depends(get_rt)) -> Task:
    return await rt.tasks.get(task_id)


@router.patch("/{task_id}")
async def update_task(task_id: str, body: TaskUpdate, rt: Runtime = Depends(get_rt)) -> Task:
    return await rt.tasks.update(task_id, body)


@router.post("/{task_id}/start")
async def start_task(task_id: str, rt: Runtime = Depends(get_rt)) -> Task:
    return await rt.scheduler.start_task(task_id)


@router.post("/{task_id}/pause")
async def pause_task(task_id: str, rt: Runtime = Depends(get_rt)) -> Task:
    return await rt.scheduler.pause_task(task_id)


@router.post("/{task_id}/resume")
async def resume_task(task_id: str, rt: Runtime = Depends(get_rt)) -> Task:
    return await rt.scheduler.resume_task(task_id)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, rt: Runtime = Depends(get_rt)) -> Task:
    return await rt.scheduler.cancel_task(task_id)


@router.post("/{task_id}/retry")
async def retry_task(task_id: str, rt: Runtime = Depends(get_rt)) -> Task:
    return await rt.scheduler.retry_task(task_id)


@router.get("/{task_id}/events")
async def task_events(
    task_id: str, after_seq: int = 0, limit: int = Query(500, ge=1, le=5000), rt: Runtime = Depends(get_rt)
) -> list[Event]:
    await rt.tasks.get(task_id)
    return await rt.ctx.bus.replay(after_seq, limit=limit, task_id=task_id)


@router.get("/{task_id}/transitions")
async def task_transitions(task_id: str, rt: Runtime = Depends(get_rt)) -> list[StateTransition]:
    await rt.tasks.get(task_id)
    return await rt.ctx.db.read(lambda tx: tasks_repo.transitions(tx, task_id))


@router.get("/{task_id}/runs")
async def task_runs(task_id: str, rt: Runtime = Depends(get_rt)) -> list[Run]:
    await rt.tasks.get(task_id)
    return await rt.ctx.db.read(lambda tx: runs_repo.for_task(tx, task_id))


@router.get("/{task_id}/checkpoints")
async def task_checkpoints(task_id: str, rt: Runtime = Depends(get_rt)) -> list[Checkpoint]:
    await rt.tasks.get(task_id)
    return await rt.checkpoints.list(task_id)


class ManualCheckpoint(BaseModel):
    label: str = Field(default="manual", max_length=200)


@router.post("/{task_id}/checkpoints", status_code=201)
async def create_checkpoint(task_id: str, body: ManualCheckpoint, rt: Runtime = Depends(get_rt)) -> Checkpoint:
    task = await rt.tasks.get(task_id)
    if task_id in rt.scheduler.running_task_ids:
        raise Conflict("pause the task before taking a manual checkpoint")
    return await rt.checkpoints.create(task, kind="manual", label=body.label)


@router.post("/{task_id}/checkpoints/{checkpoint_id}/restore")
async def restore_checkpoint(task_id: str, checkpoint_id: str, rt: Runtime = Depends(get_rt)) -> Checkpoint:
    await rt.tasks.get(task_id)
    existing = await rt.ctx.db.read(lambda tx: records.checkpoint_get(tx, checkpoint_id))
    if existing is None or existing.task_id != task_id:
        raise NotFound("checkpoint not found for this task")
    if task_id in rt.scheduler.running_task_ids:
        raise Conflict("pause the task before restoring a checkpoint")
    return await rt.checkpoints.restore(checkpoint_id)


@router.get("/{task_id}/verification")
async def task_verification(task_id: str, rt: Runtime = Depends(get_rt)) -> list[Verification]:
    await rt.tasks.get(task_id)
    return await rt.ctx.db.read(lambda tx: records.verifications_for_task(tx, task_id))


async def _worktree(rt: Runtime, task: Task) -> str:
    if not task.worktree_path or not os.path.isdir(task.worktree_path):
        raise NotFound("task has no workspace yet")
    roots = await rt.projects.roots()
    return str(resolve_inside(task.worktree_path, roots))


@router.get("/{task_id}/diff")
async def task_diff(task_id: str, rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    task = await rt.tasks.get(task_id)
    wt = await _worktree(rt, task)
    try:
        if not await git.agit(git.is_repo, wt):
            return {"available": False, "reason": "workspace is not a git repository", "changed_files": []}
        base = task.base_commit
        stat = await git.agit(git.diff, wt, base, stat=True, max_chars=20_000)
        diff = await git.agit(git.diff, wt, base)
        changed = await git.agit(git.changed_files, wt, base)
        head = await git.agit(git.rev_parse, wt, "HEAD")
    except git.WorktreeBusy:
        # A sandboxed verification command is running in the worktree: no git runs there meanwhile (N2 S2).
        return {"available": False, "busy": True, "reason": "an agent or verification is running; try again shortly",
                "changed_files": []}
    except git.UnsafeWorktree as exc:
        return {"available": False, "unsafe": True, "reason": exc.detail, "changed_files": []}
    return {
        "available": True,
        "base_commit": base,
        "head_commit": head,
        "branch": task.branch,
        "stat": redact(stat),
        "diff": redact(diff),
        "changed_files": changed,
    }


@router.get("/{task_id}/artifacts")
async def task_artifacts(task_id: str, path: Optional[str] = None, rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    """Without `path`: list changed files. With `path`: return that file's text (path policy enforced)."""
    task = await rt.tasks.get(task_id)
    wt = await _worktree(rt, task)
    if path is None:
        files: list[str] = []
        busy = False
        unsafe = None
        try:
            if await git.agit(git.is_repo, wt):
                files = await git.agit(git.changed_files, wt, task.base_commit)
        except git.WorktreeBusy:
            busy = True
        except git.UnsafeWorktree as exc:
            unsafe = exc.detail
        items = []
        for rel in files[:1000]:
            size = path_policy.file_size_inside(wt, rel)  # never follows a link planted in the worktree
            items.append({"path": rel, "exists": size is not None, "size": size})
        return {"root": wt, "files": items, **({"busy": True} if busy else {}),
                **({"unsafe": True, "reason": unsafe} if unsafe else {})}
    rel = path
    if os.path.isabs(path) or (len(path) > 1 and path[1] == ":"):
        rel = os.path.relpath(resolve_inside(path, [wt], base=wt), os.path.realpath(wt))
    # Opened without following links, containment re-checked on the handle (N2 re-review S11).
    try:
        data = path_policy.read_inside(wt, rel, max_bytes=MAX_ARTIFACT_BYTES)
    except path_policy.FileTooLarge as exc:
        raise ValidationFailed("file too large to serve", details={"size": exc.size}) from exc
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
        raise NotFound("file not found") from exc
    return {"path": path, "size": len(data), "content": redact(data.decode("utf-8", errors="replace"))}
