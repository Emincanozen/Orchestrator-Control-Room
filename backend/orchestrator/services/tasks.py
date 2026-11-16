"""Task service: creation, edits, dependency DAG and THE transition function.

Every task state change in the system goes through `transition_tx` (sync,
inside a caller transaction) or `transition` (its own transaction). It
enforces `assert_transition`, writes the task row, a `state_transitions` row
and a `task.state_changed` event atomically, and accounts active time.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from orchestrator.db import projects as projects_repo
from orchestrator.db import records
from orchestrator.db import tasks as repo
from orchestrator.db.database import Tx
from orchestrator.domain import events as ev
from orchestrator.domain.models import Task, TaskCreate, TaskUpdate
from orchestrator.domain.states import ACTIVE_STATES, TERMINAL_STATES, TaskState, assert_transition
from orchestrator.errors import Conflict, DependencyCycle, NotFound, ValidationFailed
from orchestrator.services.context import AppContext
from orchestrator.util import new_id, now_iso, seconds_between

S = TaskState
EDITABLE_STATES = frozenset({S.CREATED, S.QUEUED, S.PAUSED, S.BLOCKED, S.FAILED, S.NEEDS_APPROVAL, S.WAITING_FOR_QUOTA})


class StaleState(Conflict):
    code = "stale_state"


# --------------------------------------------------------------------------- #
# DAG
# --------------------------------------------------------------------------- #


def find_cycle(edges: dict[str, list[str]], start: str) -> Optional[list[str]]:
    """Return a dependency cycle through `start` if one exists (iterative DFS, bounded by node count)."""
    stack: list[tuple[str, list[str]]] = [(start, [start])]
    seen: set[str] = set()
    budget = sum(len(v) for v in edges.values()) + len(edges) + 1
    while stack and budget > 0:
        budget -= 1
        node, path = stack.pop()
        for nxt in edges.get(node, []):
            if nxt == start:
                return path + [start]
            if nxt not in seen:
                seen.add(nxt)
                stack.append((nxt, path + [nxt]))
    return None


def validate_dependencies_tx(tx: Tx, task_id: str, depends_on: Iterable[str]) -> list[str]:
    deps = sorted(set(depends_on))
    if task_id in deps:
        raise DependencyCycle("a task cannot depend on itself", details={"cycle": [task_id, task_id]})
    for d in deps:
        if tx.one("SELECT 1 FROM tasks WHERE id = ?", (d,)) is None:
            raise ValidationFailed(f"dependency {d} does not exist", details={"task_id": d})
    edges = repo.all_dependencies(tx)
    edges[task_id] = deps
    cycle = find_cycle(edges, task_id)
    if cycle:
        raise DependencyCycle("dependency cycle detected", details={"cycle": cycle})
    return deps


# --------------------------------------------------------------------------- #
# Transition (the only way to change task.state)
# --------------------------------------------------------------------------- #


def transition_tx(
    ctx: AppContext,
    tx: Tx,
    task_id: str,
    to_state: TaskState,
    *,
    reason: Optional[str] = None,
    actor: str = "system",
    expect_from: Optional[Iterable[TaskState]] = None,
    fields: Optional[dict[str, Any]] = None,
    active_until: Optional[str] = None,
) -> Task:
    task = repo.get(tx, task_id)
    if task is None:
        raise NotFound(f"task {task_id} not found")
    current = task.state
    if expect_from is not None and current not in set(expect_from):
        raise StaleState(
            f"task {task_id} is {current.value}, expected one of {[s.value for s in expect_from]}",
            details={"state": current.value},
        )
    assert_transition(current, to_state)
    if to_state == S.COMPLETED and not records.has_accepting_verification(tx, task_id):
        # ADR-007: a model's claim is never evidence. Enforced at the single choke point.
        raise Conflict(
            f"task {task_id} cannot complete without a passing verification record",
            code="verification_required",
        )

    ts = now_iso()
    updates: dict[str, Any] = dict(fields or {})
    updates.update(state=to_state.value, state_reason=reason, updated_at=ts, last_activity_at=ts)

    if current in ACTIVE_STATES:
        last = repo.last_transition(tx, task_id)
        if last is not None:
            end = active_until or ts
            if end < last.ts:
                end = last.ts
            updates["active_seconds"] = task.active_seconds + seconds_between(last.ts, end)
    if to_state in ACTIVE_STATES and task.started_at is None:
        updates["started_at"] = ts
    if to_state in TERMINAL_STATES or to_state == S.FAILED:
        updates["finished_at"] = ts
    elif current == S.FAILED:
        updates["finished_at"] = None

    count = tx.execute(
        "UPDATE tasks SET state = state WHERE id = ? AND state = ?", (task_id, current.value)
    ).rowcount
    if count != 1:  # defensive: cannot happen with a single serialized writer
        raise StaleState(f"task {task_id} changed concurrently")
    repo.update(tx, task_id, updates)
    repo.insert_transition(tx, task_id, current.value, to_state.value, reason, actor, ts)
    ctx.bus.append(
        tx,
        ev.TASK_STATE_CHANGED,
        project_id=task.project_id,
        task_id=task_id,
        payload={"from": current.value, "to": to_state.value, "reason": reason, "actor": actor},
    )
    return repo.get(tx, task_id)  # type: ignore[return-value]


def create_tx(ctx: AppContext, tx: Tx, dto: TaskCreate, *, actor: str = "user") -> Task:
    """Create one CREATED task (optionally autostarted) inside the caller's transaction."""
    project = projects_repo.get(tx, dto.project_id)
    if project is None:
        raise NotFound(f"project {dto.project_id} not found")
    task_id = new_id("tsk")
    ts = now_iso()
    repo.insert(
        tx,
        {
            "id": task_id,
            "project_id": dto.project_id,
            "objective_id": dto.objective_id,
            "title": dto.title,
            "description": dto.description,
            "task_type": dto.task_type,
            "priority": dto.priority,
            "state": S.CREATED.value,
            "acceptance_criteria": dto.acceptance_criteria,
            "verify_commands": dto.verify_commands,
            "plan": dto.plan,
            "pending_steps": dto.pending_steps,
            "relevant_files": dto.relevant_files,
            "role": dto.role,
            "preferred_provider": dto.preferred_provider,
            "preferred_model": dto.preferred_model,
            "created_at": ts,
            "updated_at": ts,
        },
    )
    repo.insert_transition(tx, task_id, None, S.CREATED.value, "created", actor, ts)
    if dto.depends_on:
        deps = validate_dependencies_tx(tx, task_id, dto.depends_on)
        repo.set_dependencies(tx, task_id, deps)
    task = repo.get(tx, task_id)
    assert task is not None
    ctx.bus.append(
        tx,
        ev.TASK_CREATED,
        project_id=task.project_id,
        task_id=task_id,
        payload={"task": task.model_dump(mode="json")},
    )
    if dto.autostart:
        task = transition_tx(ctx, tx, task_id, S.QUEUED, reason="autostart", actor=actor)
    return task


class TaskService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    # ---- reads -------------------------------------------------------------
    async def get(self, task_id: str) -> Task:
        task = await self.ctx.db.read(lambda tx: repo.get(tx, task_id))
        if task is None:
            raise NotFound(f"task {task_id} not found")
        return task

    async def list(self, *, project_id: Optional[str] = None, states: Optional[list[str]] = None) -> list[Task]:
        return await self.ctx.db.read(lambda tx: repo.list_tasks(tx, project_id=project_id, states=states))

    # ---- writes ------------------------------------------------------------
    async def create(self, dto: TaskCreate, *, actor: str = "user") -> Task:
        return await self.ctx.db.write(lambda tx: create_tx(self.ctx, tx, dto, actor=actor))

    async def update(self, task_id: str, dto: TaskUpdate, *, actor: str = "user") -> Task:
        changes = dto.model_dump(exclude_unset=True)

        def fn(tx: Tx) -> Task:
            task = repo.get(tx, task_id)
            if task is None:
                raise NotFound(f"task {task_id} not found")
            if task.state not in EDITABLE_STATES:
                raise Conflict(f"task {task_id} cannot be edited in state {task.state.value}")
            deps = changes.pop("depends_on", None)
            if deps is not None:
                repo.set_dependencies(tx, task_id, validate_dependencies_tx(tx, task_id, deps))
            if changes:
                repo.update(tx, task_id, {**changes, "updated_at": now_iso()})
            updated = repo.get(tx, task_id)
            assert updated is not None
            self.ctx.bus.append(
                tx,
                ev.TASK_UPDATED,
                project_id=updated.project_id,
                task_id=task_id,
                payload={"fields": sorted(list(changes) + (["depends_on"] if deps is not None else [])), "actor": actor},
            )
            return updated

        return await self.ctx.db.write(fn)

    async def set_fields(self, task_id: str, fields: dict[str, Any], *, emit: bool = False) -> Task:
        """Internal bookkeeping update (not state). Optional task.updated event."""

        def fn(tx: Tx) -> Task:
            repo.update(tx, task_id, {**fields, "updated_at": now_iso()})
            task = repo.get(tx, task_id)
            if task is None:
                raise NotFound(f"task {task_id} not found")
            if emit:
                self.ctx.bus.append(
                    tx, ev.TASK_UPDATED, project_id=task.project_id, task_id=task_id, payload={"fields": sorted(fields)}
                )
            return task

        return await self.ctx.db.write(fn)

    async def transition(
        self,
        task_id: str,
        to_state: TaskState,
        *,
        reason: Optional[str] = None,
        actor: str = "system",
        expect_from: Optional[Iterable[TaskState]] = None,
        fields: Optional[dict[str, Any]] = None,
    ) -> Task:
        expect = list(expect_from) if expect_from is not None else None
        return await self.ctx.db.write(
            lambda tx: transition_tx(
                self.ctx, tx, task_id, to_state, reason=reason, actor=actor, expect_from=expect, fields=fields
            )
        )
