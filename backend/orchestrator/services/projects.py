"""Project registration and the path-policy root set."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from orchestrator.db import projects as repo
from orchestrator.db import tasks as tasks_repo
from orchestrator.db.database import Tx
from orchestrator.domain import events as ev
from orchestrator.domain.models import Project, ProjectCreate, ProjectUpdate
from orchestrator.domain.states import TERMINAL_STATES, TaskState
from orchestrator.errors import Conflict, NotFound, ValidationFailed
from orchestrator.gitops import git
from orchestrator.security import approvals
from orchestrator.security.paths import validate_root
from orchestrator.services.context import AppContext
from orchestrator.util import new_id, now_iso


def allowed_roots_tx(tx: Tx) -> list[str]:
    """Registered project paths plus task worktrees (path policy roots)."""
    roots = [r[0] for r in tx.all("SELECT path FROM projects")]
    roots += [r[0] for r in tx.all("SELECT worktree_path FROM tasks WHERE worktree_path IS NOT NULL")]
    return roots


class ProjectService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    async def list(self) -> list[Project]:
        return await self.ctx.db.read(repo.list_all)

    async def get(self, project_id: str) -> Project:
        p = await self.ctx.db.read(lambda tx: repo.get(tx, project_id))
        if p is None:
            raise NotFound(f"project {project_id} not found")
        return p

    async def roots(self) -> list[str]:
        return await self.ctx.db.read(allowed_roots_tx)

    async def create(self, dto: ProjectCreate) -> Project:
        # Refuses drive roots, profile/system dirs and anything overlapping the data or worktrees dir (finding #7).
        # Accept non-Git registration for approval, but refuse task execution
        # until Git initialisation enables an isolated worktree.
        root = validate_root(dto.path, protected=[self.ctx.paths.data_dir, self.ctx.paths.worktrees_dir])  # + install dirs (always)
        is_git = await git.agit(git.is_repo, root)
        branch: Optional[str] = dto.default_branch
        if is_git:
            top = Path(await git.agit(git.toplevel, root)).resolve()
            if os.path.normcase(str(top)) != os.path.normcase(str(root)):
                raise ValidationFailed("register the repository root, not a subdirectory", details={"root": str(top)})
            if branch is None:
                branch = await git.agit(git.current_branch, root)

        def fn(tx: Tx) -> Project:
            if repo.get_by_path(tx, str(root)) is not None:
                raise Conflict("a project with this path is already registered")
            pid = new_id("prj")
            ts = now_iso()
            repo.insert(
                tx,
                {
                    "id": pid,
                    "name": dto.name,
                    "path": str(root),
                    "default_branch": branch,
                    "verify_commands": dto.verify_commands,
                    "skill_profile": dto.skill_profile,
                    "is_git": is_git,
                    "isolation": "worktree" if is_git else "in_place",
                    "created_at": ts,
                    "updated_at": ts,
                },
            )
            self.ctx.bus.append(
                tx,
                ev.SYSTEM_NOTICE,
                project_id=pid,
                payload={"level": "info", "message": f"project registered: {dto.name}", "is_git": is_git},
            )
            if not is_git:
                approvals.request_tx(
                    self.ctx,
                    tx,
                    approvals.KIND_GIT_INIT,
                    f"Initialise git in {root} to enable isolated worktrees (tasks remain blocked until then)",
                    project_id=pid,
                    details={"path": str(root)},
                )
            p = repo.get(tx, pid)
            assert p is not None
            return p

        return await self.ctx.db.write(fn)

    async def update(self, project_id: str, dto: ProjectUpdate) -> Project:
        changes = dto.model_dump(exclude_unset=True)

        def fn(tx: Tx) -> Project:
            if repo.get(tx, project_id) is None:
                raise NotFound(f"project {project_id} not found")
            if changes:
                repo.update(tx, project_id, {**changes, "updated_at": now_iso()})
            p = repo.get(tx, project_id)
            assert p is not None
            return p

        return await self.ctx.db.write(fn)

    async def delete(self, project_id: str) -> None:
        def fn(tx: Tx) -> None:
            if repo.get(tx, project_id) is None:
                raise NotFound(f"project {project_id} not found")
            live = [
                t.id
                for t in tasks_repo.list_tasks(tx, project_id=project_id)
                if t.state not in TERMINAL_STATES and t.state != TaskState.FAILED
            ]
            if live:
                raise Conflict("project has unfinished tasks; cancel them first", details={"task_ids": live})
            repo.delete(tx, project_id)
            self.ctx.bus.append(
                tx, ev.SYSTEM_NOTICE, project_id=project_id, payload={"level": "info", "message": "project deleted"}
            )

        await self.ctx.db.write(fn)
