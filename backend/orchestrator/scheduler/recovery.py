"""Startup recovery (ARCHITECTURE §8).

Runs BEFORE the scheduler starts:
1. Runs left in starting/running become `interrupted`. A stored PID/time
   window never authorises termination: a live or unreadable PID is uncertain.
   The previous Windows worker job owns normal crash cleanup, not PID guesses.
2. Tasks left in an active state -> QUEUED with a handoff checkpoint when the
   work is isolated in an orchestrator worktree (resumable), or BLOCKED when
   side effects are uncertain (in-place project, or an orphan we could not kill).
3. Seats showing work are reset (no process survives a restart).
4. One `system.recovery` event summarises what happened.

Requeued tasks get a fresh idempotency key on their next attempt; the
interrupted run's key stays consumed, so the same attempt never runs twice.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import psutil

from orchestrator.checkpoints.manager import CheckpointManager
from orchestrator.db import projects as projects_repo
from orchestrator.db import runs as runs_repo
from orchestrator.db import tasks as tasks_repo
from orchestrator.db.database import Tx
from orchestrator.domain import events as ev
from orchestrator.domain.models import Run
from orchestrator.domain.states import ACTIVE_STATES, RunStatus, TaskState
from orchestrator.gitops import git, worktrees
from orchestrator.security.approvals import SECURITY_REASON_PREFIX
from orchestrator.security.paths import PathPolicyError, directory_guard, is_within
from orchestrator.services.agents import AgentService
from orchestrator.services.context import AppContext
from orchestrator.services.tasks import transition_tx
from orchestrator.util import now_iso

log = logging.getLogger(__name__)
S = TaskState
UNVERIFIED_PID = "recovery: recorded PID ownership is unverified; process not terminated"
RESOLVED_PID = "recovery: recorded PID confirmed absent; restart to clear quarantine, then explicitly retry"


def pid_alive_for_run(run: Run, own_pid: int) -> bool:
    """Possible liveness only, NEVER ownership or permission to kill.

    Only a confirmed absent/zombie process is dead. An unreadable process or
    any creation-time mismatch is uncertainty, not a safe automatic requeue.
    """
    if not run.pid or run.pid == own_pid:
        return False
    try:
        proc = psutil.Process(run.pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return True
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return True


async def _retire_uncertainty(ctx: AppContext, run: Run) -> None:
    """Once absence is proven, future reuse of that PID is unrelated history."""
    def retire(tx: Tx) -> None:
        current = runs_repo.get(tx, run.id)
        if current is None or current.error_message != UNVERIFIED_PID:
            return
        runs_repo.update(tx, run.id, {"error_message": RESOLVED_PID})
        ctx.bus.append(tx, ev.SYSTEM_NOTICE, task_id=run.task_id, run_id=run.id,
                       payload={"level": "info", "message": RESOLVED_PID})
    await ctx.db.write(retire)


async def has_unverified_live_process(ctx: AppContext, task_id: str) -> bool:
    """A persisted uncertainty cannot be erased by retry or server restart."""
    runs = await ctx.db.read(lambda tx: [Run.from_row(row) for row in tx.all(
        "SELECT * FROM runs WHERE task_id = ? AND status = 'interrupted' AND error_message = ?",
        (task_id, UNVERIFIED_PID),
    )])
    uncertain = False
    for run in runs:
        if pid_alive_for_run(run, os.getpid()):
            uncertain = True
        else:
            await _retire_uncertainty(ctx, run)
    return uncertain


def _quarantine_owned_task(ctx: AppContext, task: Any, project: Any) -> bool:
    """Quarantine only the exact task path with trusted repository registration.

    Never quarantine a source checkout, a DB-supplied unrelated path or a link.
    """
    if project is None or not task.worktree_path:
        return False
    try:
        expected = worktrees.worktree_path(ctx.paths.worktrees_dir, task.id)
        if os.path.normcase(os.path.abspath(task.worktree_path)) != os.path.normcase(str(expected.absolute())):
            return False
        with directory_guard(ctx.paths.worktrees_dir), directory_guard(expected):
            if not is_within(os.path.realpath(expected), os.path.realpath(ctx.paths.worktrees_dir)):
                return False
            git.register_worktree(project.path, expected)
            git.quarantine_worktree(expected, UNVERIFIED_PID)
            return True
    except (OSError, ValueError, PathPolicyError, git.GitError):
        return False  # persistent task barrier still blocks provider execution


async def recover(ctx: AppContext, agents: Optional[AgentService] = None, *, own_pid: Optional[int] = None) -> dict[str, Any]:
    own_pid = own_pid or os.getpid()
    live_runs = await ctx.db.read(runs_repo.live)
    report: dict[str, Any] = {"interrupted_runs": [], "killed_orphans": [], "requeued": [], "blocked": [], "stale_worktrees": [], "quarantined": []}
    unresolved = await ctx.db.read(lambda tx: [Run.from_row(row) for row in tx.all(
        "SELECT * FROM runs WHERE status = 'interrupted' AND error_message = ?", (UNVERIFIED_PID,),
    )])
    orphan_tasks: set[str] = set()
    for run in [*live_runs, *unresolved]:
        alive = pid_alive_for_run(run, own_pid)
        if alive and run.task_id:
            orphan_tasks.add(run.task_id)
        if run.status == RunStatus.INTERRUPTED:
            if not alive:
                await _retire_uncertainty(ctx, run)
            continue  # reapply the persistent barrier, not duplicate run events
        reason = UNVERIFIED_PID if alive else "orchestrator restarted; process not alive"

        def mark(tx: Tx, r: Run = run, why: str = reason) -> None:
            runs_repo.update(
                tx, r.id,
                {"status": RunStatus.INTERRUPTED.value, "error_category": "crashed", "error_message": why, "finished_at": now_iso()},
            )
            ctx.bus.append(
                tx, ev.RUN_FINISHED, task_id=r.task_id, run_id=r.id, agent_id=r.agent_id,
                payload={"status": RunStatus.INTERRUPTED.value, "success": False, "error_category": "crashed", "error_message": why},
            )

        await ctx.db.write(mark)
        report["interrupted_runs"].append(run.id)

    for task_id in orphan_tasks:
        task = await ctx.db.read(lambda tx, tid=task_id: tasks_repo.get(tx, tid))
        if task is not None:
            project = await ctx.db.read(lambda tx, pid=task.project_id: projects_repo.get(tx, pid))
            if await git.agit(_quarantine_owned_task, ctx, task, project):
                report["quarantined"].append(task_id)

    active = await ctx.db.read(lambda tx: tasks_repo.list_tasks(tx, states=[s.value for s in ACTIVE_STATES]))
    cps = CheckpointManager(ctx)
    for task in active:
        project = await ctx.db.read(lambda tx, pid=task.project_id: projects_repo.get(tx, pid))
        isolated = bool(
            project is not None
            and project.isolation == "worktree"
            and task.worktree_path
            and is_within(os.path.realpath(task.worktree_path), ctx.paths.worktrees_dir)
            and os.path.isdir(task.worktree_path)
        )
        # A crash during verification loses the in-memory comparison/cleanup state.
        # A completed provider run does not establish that this payload was verified.
        interrupted_verification = task.state == S.TESTING
        if isolated and task.id not in orphan_tasks and not interrupted_verification:
            await ctx.db.write(
                lambda tx, t=task: transition_tx(
                    ctx, tx, t.id, S.QUEUED, actor="recovery",
                    reason="recovered after restart; resuming from checkpoint", active_until=t.last_activity_at,
                )
            )
            fresh = await ctx.db.read(lambda tx, tid=task.id: tasks_repo.get(tx, tid))
            if fresh is not None:
                await cps.create(fresh, kind="handoff", label="recovery after restart")
            report["requeued"].append(task.id)
        else:
            if task.id in orphan_tasks:
                why = f"{SECURITY_REASON_PREFIX} {UNVERIFIED_PID}"
            elif interrupted_verification:
                why = (f"{SECURITY_REASON_PREFIX} recovery: verification interrupted by restart; "
                       "payload unverified, human review required before explicit retry")
            else:
                why = "recovery: work ran in place; side effects uncertain"
            await ctx.db.write(
                lambda tx, t=task, w=why: transition_tx(
                    ctx, tx, t.id, S.BLOCKED, actor="recovery", reason=f"{w}; review and retry", active_until=t.last_activity_at,
                )
            )
            report["blocked"].append(task.id)

    if agents is not None:
        report["seats_reset"] = await agents.reset_working_seats()

    # Stale worktrees are reported, not deleted (deletion is a user decision).
    projects = await ctx.db.read(projects_repo.list_all)
    live_ids = await ctx.db.read(
        lambda tx: [r[0] for r in tx.all("SELECT id FROM tasks WHERE state NOT IN ('COMPLETED','CANCELLED')")]
    )
    for p in projects:
        if p.is_git and os.path.isdir(p.path):
            try:
                report["stale_worktrees"] += await git.agit(worktrees.stale, p.path, ctx.paths.worktrees_dir, live_ids)
            except git.GitError:
                pass

    if any(report[k] for k in ("interrupted_runs", "requeued", "blocked", "quarantined")) or report["stale_worktrees"]:
        await ctx.bus.emit(ev.SYSTEM_RECOVERY, payload=report)
    return report
