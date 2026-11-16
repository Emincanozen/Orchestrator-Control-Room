"""Structured checkpoints: Handoff summary + git ref, and restore."""

from __future__ import annotations

import os
from typing import Optional

from orchestrator.checkpoints.handoff import build_handoff
from orchestrator.db import records
from orchestrator.db import tasks as tasks_repo
from orchestrator.db.database import Tx
from orchestrator.domain import events as ev
from orchestrator.domain.models import Checkpoint, CheckpointKind, Task
from orchestrator.domain.states import ACTIVE_STATES
from orchestrator.errors import Conflict, NotFound
from orchestrator.gitops import git
from orchestrator.security.paths import is_within
from orchestrator.services.context import AppContext
from orchestrator.util import new_id, now_iso


class CheckpointManager:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    def _owned_worktree(self, task: Task) -> Optional[str]:
        wt = task.worktree_path
        if wt and os.path.isdir(wt) and is_within(os.path.realpath(wt), self.ctx.paths.worktrees_dir):
            return wt
        return None

    async def create(
        self,
        task: Task,
        *,
        kind: CheckpointKind = "auto",
        run_id: Optional[str] = None,
        label: str = "",
        commit: bool = True,
    ) -> Checkpoint:
        git_ref: Optional[str] = None
        commit_error: Optional[str] = None
        wt = self._owned_worktree(task)
        if wt is not None:
            try:
                if commit:
                    message = f"orchestrator checkpoint ({kind}) {task.id}{': ' + label if label else ''}"
                    await git.agit(git.commit_all, wt, message)
                git_ref = await git.agit(git.rev_parse, wt, "HEAD")
            except git.UnsafeWorktree:
                raise  # a security finding (embedded repository): the executor blocks the task
            except git.GitError as exc:
                commit_error = str(exc)
        handoff = await build_handoff(self.ctx, task)
        summary = handoff.model_dump(mode="json")
        if label:
            summary["label"] = label
        if commit_error:
            summary["git_error"] = commit_error
        cid = new_id("chk")

        def fn(tx: Tx) -> Checkpoint:
            records.checkpoint_insert(
                tx,
                {"id": cid, "task_id": task.id, "run_id": run_id, "kind": kind, "summary": summary, "git_ref": git_ref, "created_at": now_iso()},
            )
            if git_ref:
                tasks_repo.update(tx, task.id, {"head_commit": git_ref, "updated_at": now_iso()})
            self.ctx.bus.append(
                tx,
                ev.CHECKPOINT_CREATED,
                project_id=task.project_id,
                task_id=task.id,
                run_id=run_id,
                payload={"checkpoint_id": cid, "kind": kind, "git_ref": git_ref, "label": label, "git_error": commit_error},
            )
            cp = records.checkpoint_get(tx, cid)
            assert cp is not None
            return cp

        return await self.ctx.db.write(fn)

    async def list(self, task_id: str) -> list[Checkpoint]:
        return await self.ctx.db.read(lambda tx: records.checkpoints_for_task(tx, task_id))

    async def latest(self, task_id: str) -> Optional[Checkpoint]:
        cps = await self.list(task_id)
        return cps[-1] if cps else None

    async def restore(self, checkpoint_id: str) -> Checkpoint:
        """Reset the task's own worktree to the checkpoint's git ref. Never touches the project path."""
        cp = await self.ctx.db.read(lambda tx: records.checkpoint_get(tx, checkpoint_id))
        if cp is None:
            raise NotFound(f"checkpoint {checkpoint_id} not found")
        task = await self.ctx.db.read(lambda tx: tasks_repo.get(tx, cp.task_id))
        if task is None:
            raise NotFound(f"task {cp.task_id} not found")
        if task.state in ACTIVE_STATES:
            raise Conflict("cannot restore while the task is active; pause it first")
        wt = self._owned_worktree(task)
        if wt is None or not cp.git_ref:
            raise Conflict("checkpoint has no git ref or task has no orchestrator-owned worktree")
        await git.agit(git.reset_hard, wt, cp.git_ref)

        def fn(tx: Tx) -> None:
            tasks_repo.update(tx, task.id, {"head_commit": cp.git_ref, "updated_at": now_iso()})
            self.ctx.bus.append(
                tx,
                ev.SYSTEM_NOTICE,
                project_id=task.project_id,
                task_id=task.id,
                payload={"level": "info", "message": f"worktree restored to checkpoint {checkpoint_id}", "git_ref": cp.git_ref},
            )

        await self.ctx.db.write(fn)
        return cp
