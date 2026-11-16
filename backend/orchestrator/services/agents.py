"""Office seats (agents table) and their visual activity.

`set_activity_tx` is the ONLY function that changes an agent's activity; it
always emits `agent.activity_changed`. Working activities require a live
process (provider run or verification command) — an agent never shows work
it is not doing.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from orchestrator.db import records
from orchestrator.db.database import Tx
from orchestrator.domain import events as ev
from orchestrator.domain.models import Agent, Task
from orchestrator.domain.states import AgentActivity, RunPhase, TaskState
from orchestrator.services.context import AppContext
from orchestrator.util import now_iso

A = AgentActivity
S = TaskState

ROOMS = ("leadership", "backend", "frontend", "research", "testing", "visual", "meeting", "waiting")

# (id, name, role, room)
ROSTER: tuple[tuple[str, str, str, str], ...] = (
    ("agt_leader", "Leader", "leader", "leadership"),
    ("agt_advisor", "Advisor", "advisor", "leadership"),
    ("agt_backend_1", "Backend 1", "backend", "backend"),
    ("agt_backend_2", "Backend 2", "backend", "backend"),
    ("agt_frontend_1", "Frontend 1", "frontend", "frontend"),
    ("agt_frontend_2", "Frontend 2", "frontend", "frontend"),
    ("agt_research", "Research", "research", "research"),
    ("agt_test", "Test", "test", "testing"),
    ("agt_visual", "Visual", "visual", "visual"),
    ("agt_reviewer", "Reviewer", "reviewer", "meeting"),
)

WORKING_ACTIVITIES = frozenset({A.PLANNING, A.CODING, A.RESEARCHING, A.TESTING, A.REVIEWING, A.CONSULTING})
WORKER_ROLES = ("backend", "frontend", "research", "test", "visual", "reviewer")

_TYPE_TO_ROLE = {
    "coding": "backend",
    "backend": "backend",
    "frontend": "frontend",
    "research": "research",
    "documentation": "research",
    "testing": "test",
    "browser_check": "test",
    "visual_asset": "visual",
    "review": "reviewer",
    "planning": "leader",
    "architecture": "leader",
}
_ROLE_ALIASES = {"core_systems": "backend", "pixel_office": "frontend", "security": "reviewer", "test": "test"}


LEADER_SEAT = "agt_leader"
ADVISOR_SEAT = "agt_advisor"


def leadership_seat(phase: RunPhase) -> Optional[str]:
    """Plan/review (leader) and advise (advisor) runs show on the leadership seats, not on a worker seat."""
    if phase in (RunPhase.PLAN, RunPhase.REVIEW):
        return LEADER_SEAT
    if phase == RunPhase.ADVISE:
        return ADVISOR_SEAT
    return None


class ActivityError(ValueError):
    pass


def seat_role_for(task: Task) -> str:
    seat_roles = {r[2] for r in ROSTER}
    if task.role:
        role = _ROLE_ALIASES.get(task.role, task.role)
        if role in seat_roles:
            return role
    return _TYPE_TO_ROLE.get(task.task_type, "backend")


def activity_for(state: TaskState, *, phase: Optional[RunPhase] = None, task_type: str = "coding") -> AgentActivity:
    """Execution state -> office activity (spec item 9)."""
    if phase == RunPhase.ADVISE:
        return A.CONSULTING
    if phase == RunPhase.PLAN:
        return A.PLANNING
    if phase == RunPhase.REVIEW:
        return A.REVIEWING
    if state == S.PLANNING:
        return A.PLANNING
    if state == S.RUNNING:
        return A.RESEARCHING if task_type in ("research", "documentation") else A.CODING
    if state == S.TESTING:
        return A.TESTING
    if state == S.REVIEWING:
        return A.REVIEWING
    if state == S.WAITING_FOR_QUOTA:
        return A.RATE_LIMITED
    if state == S.NEEDS_APPROVAL:
        return A.NEEDS_APPROVAL
    if state == S.BLOCKED:
        return A.BLOCKED
    if state == S.COMPLETED:
        return A.CELEBRATING
    return A.IDLE  # QUEUED / PAUSED / CREATED / FAILED / CANCELLED: no process


def seed_tx(tx: Tx) -> int:
    if records.agent_count(tx) > 0:
        return 0
    ts = now_iso()
    for order, (aid, name, role, room) in enumerate(ROSTER):
        records.agent_insert(
            tx,
            {"id": aid, "name": name, "role": role, "room": room, "activity": A.IDLE.value, "since": ts, "seat_order": order},
        )
    return len(ROSTER)


def set_activity_tx(
    ctx: AppContext,
    tx: Tx,
    agent_id: str,
    activity: AgentActivity,
    *,
    live: bool = False,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
    operation: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    project_id: Optional[str] = None,
    keep_task: bool = False,
) -> Agent:
    agent = records.agent_get(tx, agent_id)
    if agent is None:
        raise ActivityError(f"agent {agent_id} not found")
    if activity in WORKING_ACTIVITIES and not live:
        raise ActivityError(f"activity {activity.value} requires a live process")
    if activity in WORKING_ACTIVITIES and run_id is None and operation is None:
        raise ActivityError("working activity needs a run_id or an operation")
    fields: dict = {"activity": activity.value, "since": now_iso()}
    if activity in (A.IDLE, A.OFFLINE):
        # keep_task: process ended between phases; the seat stays reserved but shows no work.
        keep = (task_id or agent.current_task_id) if (keep_task and activity == A.IDLE) else None
        fields.update(current_task_id=keep, current_run_id=None, current_operation=None, provider=None, model=None)
    else:
        fields.update(
            current_task_id=task_id if task_id is not None else agent.current_task_id,
            current_run_id=run_id if activity in WORKING_ACTIVITIES else None,
            current_operation=operation if activity in WORKING_ACTIVITIES else None,
        )
        if activity in WORKING_ACTIVITIES:
            fields.update(provider=provider, model=model)
    records.agent_update(tx, agent_id, fields)
    updated = records.agent_get(tx, agent_id)
    assert updated is not None
    ctx.bus.append(
        tx,
        ev.AGENT_ACTIVITY_CHANGED,
        project_id=project_id,
        task_id=updated.current_task_id or task_id,
        run_id=updated.current_run_id,
        agent_id=agent_id,
        payload={
            "from": agent.activity.value,
            "to": activity.value,
            "operation": updated.current_operation,
            "provider": updated.provider,
            "model": updated.model,
        },
    )
    return updated


def pick_seat_tx(tx: Tx, task: Task) -> Optional[str]:
    agents = records.agent_list(tx)
    for a in agents:  # same seat if it still holds this task
        if a.current_task_id == task.id:
            return a.id
    role = seat_role_for(task)
    idle = [a for a in agents if a.activity == A.IDLE and a.current_task_id is None]
    for a in idle:
        if a.role == role:
            return a.id
    for a in idle:
        if a.role in WORKER_ROLES:
            return a.id
    # Seats parked on a waiting/blocked task can be reused; that task gets a seat again later.
    # Seats reserved by a task that is still executing are never taken.
    active_ids = {
        r[0]
        for r in tx.all(
            "SELECT id FROM tasks WHERE state IN ('QUEUED','PLANNING','RUNNING','TESTING','REVIEWING') AND id <> ?",
            (task.id,),
        )
    }
    parked = [
        a
        for a in agents
        if a.activity not in WORKING_ACTIVITIES
        and a.role in WORKER_ROLES + ("leader",)
        and a.current_task_id not in active_ids
    ]
    for a in parked:
        if a.role == role:
            return a.id
    return parked[0].id if parked else None


class AgentService:
    def __init__(self, ctx: AppContext, *, celebrate_seconds: float = 3.0):
        self.ctx = ctx
        self.celebrate_seconds = celebrate_seconds
        self._timers: set[asyncio.Task] = set()

    async def seed(self) -> int:
        return await self.ctx.db.write(seed_tx)

    async def list(self) -> list[Agent]:
        return await self.ctx.db.read(records.agent_list)

    async def get(self, agent_id: str) -> Optional[Agent]:
        return await self.ctx.db.read(lambda tx: records.agent_get(tx, agent_id))

    async def set_activity(self, agent_id: str, activity: AgentActivity, **kwargs) -> Agent:
        return await self.ctx.db.write(lambda tx: set_activity_tx(self.ctx, tx, agent_id, activity, **kwargs))

    async def celebrate(self, agent_id: str, task_id: str, project_id: Optional[str] = None) -> None:
        """Transient celebrating -> idle, only if the seat was not reassigned meanwhile."""
        await self.set_activity(agent_id, A.CELEBRATING, task_id=task_id, project_id=project_id)

        async def later() -> None:
            await asyncio.sleep(self.celebrate_seconds)

            def fn(tx: Tx) -> None:
                a = records.agent_get(tx, agent_id)
                if a and a.activity == A.CELEBRATING and a.current_task_id == task_id:
                    set_activity_tx(self.ctx, tx, agent_id, A.IDLE, project_id=project_id)

            await self.ctx.db.write(fn)

        if self.celebrate_seconds <= 0:
            await later()
            return
        t = asyncio.create_task(later())
        self._timers.add(t)
        t.add_done_callback(self._timers.discard)

    async def reset_working_seats(self) -> int:
        """Startup: no process survives a restart, so no seat may show work."""

        def fn(tx: Tx) -> int:
            n = 0
            for a in records.agent_list(tx):
                if a.activity in WORKING_ACTIVITIES or a.activity == A.CELEBRATING:
                    set_activity_tx(self.ctx, tx, a.id, A.IDLE)
                    n += 1
            return n

        return await self.ctx.db.write(fn)

    async def shutdown(self) -> None:
        for t in list(self._timers):
            t.cancel()
        self._timers.clear()
