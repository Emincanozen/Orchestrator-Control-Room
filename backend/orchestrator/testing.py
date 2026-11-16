"""Test helpers (used by backend/tests; no production code imports this).

Builds an isolated runtime in a temp data dir with a MockProvider and real
git repositories.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from orchestrator.config import get_paths
from orchestrator.db import records
from orchestrator.domain.models import ProjectCreate, Task, TaskCreate
from orchestrator.domain.states import TaskState
from orchestrator.providers.mock import MockProvider
from orchestrator.scheduler.executor import TaskExecutor
from orchestrator.scheduler.scheduler import Scheduler
from orchestrator.security.approvals import ApprovalService
from orchestrator.services.agents import AgentService
from orchestrator.services.context import AppContext
from orchestrator.services.projects import ProjectService
from orchestrator.services.tasks import TaskService

PY = sys.executable

FAST_LIMITS = {
    "retry_backoff_seconds": 0,
    "quota_recheck_seconds": 300,
    "run_heartbeat_timeout_seconds": 30,
    "max_active_runtime_seconds": 60,
    "verify_timeout_seconds": 60,
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def make_git_repo(path: Path, files: Optional[dict[str, str]] = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--quiet", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    for rel, content in (files or {"README.md": "# test repo\n"}).items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "initial")
    return path.resolve()


@dataclass
class Harness:
    ctx: AppContext
    mock: MockProvider
    agents: AgentService
    executor: TaskExecutor
    scheduler: Scheduler
    tasks: TaskService
    projects: ProjectService
    approvals: ApprovalService
    tmp: Path
    providers: dict[str, Any] = field(default_factory=dict)

    async def set_limits(self, **limits: Any) -> None:
        current = await self.ctx.db.read(records.settings_all)
        merged = {**(current.get("limits") or {}), **limits}
        await self.ctx.db.write(lambda tx: records.setting_put(tx, "limits", merged))

    async def project(self, name: str = "demo", files: Optional[dict[str, str]] = None, git_repo: bool = True, **kw: Any):
        path = self.tmp / f"proj-{name}"
        if git_repo:
            make_git_repo(path, files)
        else:
            path.mkdir(parents=True, exist_ok=True)
            for rel, content in (files or {}).items():
                (path / rel).write_text(content, encoding="utf-8")
        return await self.projects.create(ProjectCreate(name=name, path=str(path), **kw))

    async def task(self, project_id: str, **kw: Any) -> Task:
        kw.setdefault("title", "demo task")
        kw.setdefault("preferred_provider", "mock")
        return await self.tasks.create(TaskCreate(project_id=project_id, **kw))

    async def wait_state(self, task_id: str, states: Iterable[TaskState], timeout: float = 30.0) -> Task:
        wanted = set(states)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            t = await self.tasks.get(task_id)
            if t.state in wanted:
                return t
            await asyncio.sleep(0.05)
        t = await self.tasks.get(task_id)
        raise AssertionError(f"task {task_id} stayed {t.state.value} ({t.state_reason}); wanted {[s.value for s in wanted]}")

    async def run_once(self, task_id: str) -> TaskState:
        """Run one executor pass synchronously (no scheduler loop)."""
        state = await self.executor.run_task(task_id)
        assert state is not None
        return state

    async def close(self) -> None:
        await self.scheduler.stop(grace_seconds=5)
        await self.agents.shutdown()
        self.ctx.close()


async def make_harness(tmp_path: Path, *, mock: Optional[MockProvider] = None, limits: Optional[dict[str, Any]] = None, **executor_kw: Any) -> Harness:
    paths = get_paths(tmp_path / "data")
    ctx = AppContext.open(paths)
    mock = mock or MockProvider()
    providers = {"mock": mock}
    agents = AgentService(ctx, celebrate_seconds=0)
    await agents.seed()
    executor = TaskExecutor(ctx, providers, agents=agents, **executor_kw)
    scheduler = Scheduler(ctx, executor, poll_interval=0.05)
    h = Harness(
        ctx=ctx, mock=mock, agents=agents, executor=executor, scheduler=scheduler, tasks=TaskService(ctx),
        projects=ProjectService(ctx), approvals=ApprovalService(ctx), tmp=tmp_path, providers=providers,
    )
    await h.set_limits(**{**FAST_LIMITS, **(limits or {})})
    return h


def py_check(code: str) -> list[str]:
    """List-form verify command running the current interpreter."""
    return [PY, "-c", code]


# Cheap deterministic verification specs for tests that do not assert anything about verification itself:
# a file check needs no interpreter process (saves ~0.2-0.5 s per verification on Windows).
PASS_CHECK = [{"file_exists": "README.md"}]  # make_git_repo's default README is in every worktree
FAIL_CHECK = [{"file_exists": "__orchestrator_missing_file__"}]
