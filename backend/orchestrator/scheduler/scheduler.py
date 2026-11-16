"""Async scheduler loop (ARCHITECTURE §8).

Durable queue = tasks WHERE state='QUEUED' ORDER BY priority DESC, created_at.
A task is runnable when all dependencies are COMPLETED, its not_before has
passed, a global and per-project slot is free and no other active task uses
the same worktree (in-place projects therefore run one task at a time).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable, Optional

from orchestrator.db import projects as projects_repo
from orchestrator.db import tasks as tasks_repo
from orchestrator.db.database import Tx
from orchestrator.domain import events as ev
from orchestrator.domain.models import Project, Task
from orchestrator.domain.states import TERMINAL_STATES, TaskState, can_transition
from orchestrator.errors import Conflict
from orchestrator.scheduler.executor import TaskExecutor
from orchestrator.services.context import AppContext
from orchestrator.services.tasks import TaskService, transition_tx
from orchestrator.util import now_iso

log = logging.getLogger(__name__)
S = TaskState

DEP_BLOCK_PREFIX = "dependency "
MAX_TICK_CANDIDATES = 1000

QuotaCheck = Callable[[Task], Awaitable[bool]]


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


class Scheduler:
    def __init__(
        self,
        ctx: AppContext,
        executor: TaskExecutor,
        *,
        poll_interval: float = 1.0,
        quota_check: Optional[QuotaCheck] = None,
    ):
        self.ctx = ctx
        self.executor = executor
        self.tasks = TaskService(ctx)
        self.poll_interval = poll_interval
        self.quota_check = quota_check
        self._running: dict[str, asyncio.Task] = {}
        self._running_meta: dict[str, tuple[str, str]] = {}  # task_id -> (project_id, worktree key)
        self._wake: Optional[asyncio.Event] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._stopping = False
        self.assignment_paused = False  # leader switch protocol pauses new assignments

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self._wake = asyncio.Event()
        self._stopping = False
        self._loop_task = asyncio.create_task(self._loop(), name="scheduler-loop")

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    @property
    def running_task_ids(self) -> list[str]:
        return sorted(self._running)

    @property
    def is_running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    async def stop(self, grace_seconds: float = 15.0) -> None:
        """Graceful shutdown: stop assigning, ask live runs to stop (requeue), then cancel leftovers."""
        self._stopping = True
        self.wake()
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        for tid in list(self._running):
            await self.executor.request_stop(tid, "shutdown")
        pending = list(self._running.values())
        if pending:
            _done, still = await asyncio.wait(pending, timeout=grace_seconds)
            for t in still:
                t.cancel()
            if still:
                await asyncio.wait(still, timeout=10)

    async def _loop(self) -> None:
        assert self._wake is not None
        failures = 0
        # Service loop: runs for the process lifetime and ends on stop(); each
        # iteration does a bounded amount of work.
        while not self._stopping:
            try:
                await self.tick()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                log.exception("scheduler tick failed (%d)", failures)
            self._wake.clear()
            delay = self.poll_interval * min(30, 2 ** min(failures, 5)) if failures else self.poll_interval
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ #
    # One scheduling pass
    # ------------------------------------------------------------------ #
    async def tick(self) -> list[str]:
        now = now_iso()
        await self._release_quota_waits(now)
        await self._dependency_gating()
        if self._stopping or self.assignment_paused:
            return []
        policies = await self.ctx.policies()
        max_global = policies.int_limit("max_parallel_workers")
        max_project = policies.int_limit("max_parallel_per_project")

        def load(tx: Tx) -> tuple[list[Task], dict[str, Project], dict[str, list[str]], dict[str, str]]:
            queued = tasks_repo.list_tasks(tx, states=[S.QUEUED.value], limit=MAX_TICK_CANDIDATES)
            projects = {p.id: p for p in projects_repo.list_all(tx)}
            deps = tasks_repo.all_dependencies(tx)
            states = {r[0]: r[1] for r in tx.all("SELECT id, state FROM tasks")}
            return queued, projects, deps, states

        queued, projects, deps, states = await self.ctx.db.read(load)
        started: list[str] = []
        per_project: dict[str, int] = {}
        busy_worktrees: set[str] = set()
        for pid, wt in self._running_meta.values():
            per_project[pid] = per_project.get(pid, 0) + 1
            busy_worktrees.add(wt)
        for task in queued:
            if len(self._running) >= max_global:
                break
            if task.id in self._running:
                continue
            if task.not_before and task.not_before > now:
                continue
            if any(states.get(d) != S.COMPLETED.value for d in deps.get(task.id, [])):
                continue
            project = projects.get(task.project_id)
            if project is None:
                continue
            limit = 1 if project.isolation == "in_place" or not project.is_git else max_project
            if per_project.get(project.id, 0) >= limit:
                continue
            wt_key = _norm(self._worktree_for(task, project))
            if wt_key in busy_worktrees:
                continue
            self._spawn(task, project.id, wt_key)
            per_project[project.id] = per_project.get(project.id, 0) + 1
            busy_worktrees.add(wt_key)
            started.append(task.id)
        return started

    def _worktree_for(self, task: Task, project: Project) -> str:
        if project.isolation == "in_place" or not project.is_git:
            return project.path
        return task.worktree_path or str(self.ctx.paths.worktrees_dir / task.id)

    def _spawn(self, task: Task, project_id: str, wt_key: str) -> None:
        t = asyncio.create_task(self.executor.run_task(task.id), name=f"exec-{task.id}")
        self._running[task.id] = t
        self._running_meta[task.id] = (project_id, wt_key)

        def done(_t: asyncio.Task, tid: str = task.id) -> None:
            self._running.pop(tid, None)
            self._running_meta.pop(tid, None)
            if not _t.cancelled() and _t.exception() is not None:
                log.error("executor task for %s crashed: %r", tid, _t.exception())
            self.wake()

        t.add_done_callback(done)

    async def wait_idle(self, timeout: float = 30.0) -> None:
        """Test helper: wait until no executor task is running."""
        pending = list(self._running.values())
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def _release_quota_waits(self, now: str) -> None:
        waiting = await self.ctx.db.read(lambda tx: tasks_repo.list_tasks(tx, states=[S.WAITING_FOR_QUOTA.value]))
        for task in waiting:
            if task.not_before and task.not_before > now:
                continue
            if self.quota_check is not None and not await self.quota_check(task):
                continue
            try:
                await self.tasks.transition(
                    task.id, S.QUEUED, reason="quota recheck passed", actor="scheduler",
                    expect_from=[S.WAITING_FOR_QUOTA], fields={"not_before": None},
                )
            except Conflict:
                pass

    async def _dependency_gating(self) -> None:
        def fn(tx: Tx) -> None:
            deps = tasks_repo.all_dependencies(tx)
            if not deps:
                return
            states = {r[0]: r[1] for r in tx.all("SELECT id, state FROM tasks")}
            bad = {S.FAILED.value, S.CANCELLED.value}
            for task_id, dep_ids in deps.items():
                state = states.get(task_id)
                failed = [d for d in dep_ids if states.get(d) in bad]
                if state == S.QUEUED.value and failed:
                    transition_tx(
                        self.ctx, tx, task_id, S.BLOCKED, actor="scheduler",
                        reason=f"{DEP_BLOCK_PREFIX}{failed[0]} is {states[failed[0]]}",
                    )
                elif state == S.BLOCKED.value and not failed:
                    t = tasks_repo.get(tx, task_id)
                    if t is not None and (t.state_reason or "").startswith(DEP_BLOCK_PREFIX):
                        transition_tx(self.ctx, tx, task_id, S.QUEUED, actor="scheduler", reason="dependencies recovered")

        await self.ctx.db.write(fn)

    # ------------------------------------------------------------------ #
    # Controls (API: start/pause/resume/cancel/retry)
    # ------------------------------------------------------------------ #
    async def start_task(self, task_id: str, actor: str = "user") -> Task:
        task = await self.tasks.transition(task_id, S.QUEUED, reason="started", actor=actor, expect_from=[S.CREATED])
        self.wake()
        return task

    async def pause_task(self, task_id: str, actor: str = "user") -> Task:
        if task_id in self._running:
            await self.executor.request_stop(task_id, "pause")
            return await self.tasks.get(task_id)
        task = await self.tasks.get(task_id)
        if not can_transition(task.state, S.PAUSED):
            raise Conflict(f"cannot pause a task in state {task.state.value}")
        return await self.tasks.transition(task_id, S.PAUSED, reason="paused by user", actor=actor, expect_from=[task.state])

    async def resume_task(self, task_id: str, actor: str = "user") -> Task:
        task = await self.tasks.transition(task_id, S.QUEUED, reason="resumed", actor=actor, expect_from=[S.PAUSED])
        self.wake()
        return task

    async def cancel_task(self, task_id: str, actor: str = "user") -> Task:
        if task_id in self._running:
            await self.executor.request_stop(task_id, "cancel")
            return await self.tasks.get(task_id)
        task = await self.tasks.get(task_id)
        if task.state in TERMINAL_STATES:
            raise Conflict(f"task is already {task.state.value}")
        return await self.tasks.transition(task_id, S.CANCELLED, reason="cancelled by user", actor=actor, expect_from=[task.state])

    async def retry_task(self, task_id: str, actor: str = "user") -> Task:
        task = await self.tasks.get(task_id)
        if task.state not in (S.FAILED, S.BLOCKED):
            raise Conflict(f"only FAILED or BLOCKED tasks can be retried (state {task.state.value})")
        task = await self.tasks.transition(
            task_id, S.QUEUED, reason="explicit retry", actor=actor, expect_from=[S.FAILED, S.BLOCKED],
            fields={
                "retry_count": 0, "consecutive_failures": 0, "repair_cycles": 0, "not_before": None,
                "route_exclusions": [], "provider_switches": 0, "granted_permission": None,
            },
        )
        await self.ctx.bus.emit(ev.TASK_UPDATED, project_id=task.project_id, task_id=task_id, payload={"fields": ["retry_count", "repair_cycles"], "actor": actor})
        self.wake()
        return task
