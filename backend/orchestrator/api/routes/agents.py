"""Agent seats + inspector payload."""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Depends

from orchestrator.api.deps import AUTH, get_rt
from orchestrator.api.runtime import Runtime
from orchestrator.db import projects as projects_repo
from orchestrator.db import records
from orchestrator.db import runs as runs_repo
from orchestrator.db import tasks as tasks_repo
from orchestrator.db.database import Tx
from orchestrator.domain.models import Agent, AgentInspector, Measured
from orchestrator.errors import NotFound
from orchestrator.events.bus import event_from_row
from orchestrator.gitops import git
from orchestrator.util import now_iso, seconds_between

router = APIRouter(prefix="/api/agents", tags=["agents"], dependencies=AUTH)


@router.get("")
async def list_agents(rt: Runtime = Depends(get_rt)) -> list[Agent]:
    return await rt.agents.list()


def _sum_usage(runs: list) -> dict[str, Any]:
    total: dict[str, Any] = {}
    for r in runs:
        for k, v in (r.usage or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                total[k] = total.get(k, 0) + v
    return total


@router.get("/{agent_id}")
async def inspect_agent(agent_id: str, rt: Runtime = Depends(get_rt)) -> AgentInspector:
    def fn(tx: Tx) -> dict[str, Any]:
        agent = records.agent_get(tx, agent_id)
        if agent is None:
            raise NotFound(f"agent {agent_id} not found")
        task = tasks_repo.get(tx, agent.current_task_id) if agent.current_task_id else None
        project = projects_repo.get(tx, task.project_id) if task else None
        rows = tx.all(
            "SELECT * FROM events WHERE agent_id = ? OR (task_id IS NOT NULL AND task_id = ?) ORDER BY seq DESC LIMIT 20",
            (agent_id, agent.current_task_id or ""),
        )
        runs = runs_repo.for_task(tx, task.id) if task else []
        verification = records.latest_verification(tx, task.id) if task else None
        current_run = runs_repo.get(tx, agent.current_run_id) if agent.current_run_id else None
        return {
            "agent": agent, "task": task, "project": project, "events": [event_from_row(r) for r in rows],
            "runs": runs, "verification": verification, "current_run": current_run,
        }

    d = await rt.ctx.db.read(fn)
    agent: Agent = d["agent"]
    task = d["task"]
    changed: list[str] = []
    if task and task.worktree_path and os.path.isdir(task.worktree_path):
        try:
            if await git.agit(git.is_repo, task.worktree_path):
                changed = (await git.agit(git.changed_files, task.worktree_path, task.base_commit))[:200]
        except git.GitError:
            changed = []
    v = d["verification"]
    test_results: Optional[dict[str, Any]] = None
    if v is not None:
        test_results = {
            "status": v.status,
            "at": v.finished_at,
            "checks": [{"name": c.name, "passed": c.passed, "exit_code": c.exit_code, "error": c.error} for c in v.checks],
        }
    current_run = d["current_run"]
    usage = {
        "current_run": {"value": current_run.usage, "exactness": "exact", "source": "provider events"} if current_run else None,
        "task_total": {"value": _sum_usage(d["runs"]), "exactness": "exact", "source": "sum of recorded run usage"} if task else None,
    }
    status = agent.activity.value
    if task is not None:
        status = f"{agent.activity.value} ({task.state.value})"
    return AgentInspector(
        id=agent.id,
        name=agent.name,
        role=agent.role,
        room=agent.room,
        activity=agent.activity,
        status=status,
        provider=agent.provider or (current_run.provider if current_run else None),
        model=(current_run.model if current_run and current_run.model else agent.model),
        project_id=task.project_id if task else None,
        project_name=d["project"].name if d["project"] else None,
        task_id=task.id if task else None,
        task_title=task.title if task else None,
        task_state=task.state.value if task else None,
        run_id=agent.current_run_id,
        operation=agent.current_operation,
        since=agent.since,
        elapsed_seconds=Measured(value=round(seconds_between(agent.since, now_iso()), 1), exactness="measured", source="agents.since"),
        recent_events=[e.model_dump(mode="json") for e in d["events"]],
        changed_files=changed,
        test_results=test_results,
        usage=usage,
    )
