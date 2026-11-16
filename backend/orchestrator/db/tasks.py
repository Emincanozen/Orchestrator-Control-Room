"""Tasks + task_dependencies + state_transitions repository."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from orchestrator.db import repo
from orchestrator.db.database import Tx
from orchestrator.domain.models import StateTransition, Task

JSON = ("acceptance_criteria", "verify_commands", "plan", "completed_steps", "pending_steps", "relevant_files", "route_exclusions", "granted_permission")
UPDATABLE = (
    "title",
    "description",
    "task_type",
    "priority",
    "state",
    "state_reason",
    "acceptance_criteria",
    "verify_commands",
    "plan",
    "completed_steps",
    "pending_steps",
    "next_action",
    "relevant_files",
    "role",
    "preferred_provider",
    "preferred_model",
    "attempt_count",
    "consecutive_failures",
    "retry_count",
    "repair_cycles",
    "not_before",
    "worktree_path",
    "branch",
    "base_commit",
    "head_commit",
    "active_seconds",
    "last_activity_at",
    "updated_at",
    "started_at",
    "finished_at",
    "route_exclusions",
    "provider_switches",
    "granted_permission",
)


def insert(tx: Tx, values: dict[str, Any]) -> None:
    repo.insert(tx, "tasks", values, JSON)


def _with_deps(tx: Tx, task: Optional[Task]) -> Optional[Task]:
    if task is not None:
        task.depends_on = dependencies_of(tx, task.id)
    return task


def get(tx: Tx, task_id: str) -> Optional[Task]:
    return _with_deps(tx, Task.from_row(tx.one("SELECT * FROM tasks WHERE id = ?", (task_id,))))


def list_tasks(
    tx: Tx,
    *,
    project_id: Optional[str] = None,
    states: Optional[Iterable[str]] = None,
    limit: int = 500,
) -> list[Task]:
    sql = "SELECT * FROM tasks WHERE 1=1"
    params: list[Any] = []
    if project_id:
        sql += " AND project_id = ?"
        params.append(project_id)
    states = list(states or [])
    if states:
        sql += f" AND state IN ({','.join('?' * len(states))})"
        params.extend(states)
    sql += " ORDER BY priority DESC, created_at LIMIT ?"
    params.append(max(1, min(limit, 5000)))
    rows = tx.all(sql, params)
    deps = all_dependencies(tx)
    out = []
    for r in rows:
        t = Task.from_row(r)
        t.depends_on = deps.get(t.id, [])
        out.append(t)
    return out


def update(tx: Tx, task_id: str, fields: dict[str, Any]) -> int:
    return repo.update(tx, "tasks", "id", task_id, fields, allowed=UPDATABLE, json_cols=JSON)


# ---- dependencies ----------------------------------------------------------


def dependencies_of(tx: Tx, task_id: str) -> list[str]:
    rows = tx.all("SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ? ORDER BY 1", (task_id,))
    return [r[0] for r in rows]


def dependents_of(tx: Tx, task_id: str) -> list[str]:
    rows = tx.all("SELECT task_id FROM task_dependencies WHERE depends_on_task_id = ? ORDER BY 1", (task_id,))
    return [r[0] for r in rows]


def all_dependencies(tx: Tx) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in tx.all("SELECT task_id, depends_on_task_id FROM task_dependencies ORDER BY 1, 2"):
        out.setdefault(r[0], []).append(r[1])
    return out


def set_dependencies(tx: Tx, task_id: str, depends_on: list[str]) -> None:
    tx.execute("DELETE FROM task_dependencies WHERE task_id = ?", (task_id,))
    tx.executemany(
        "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
        [(task_id, d) for d in sorted(set(depends_on))],
    )


# ---- transitions -----------------------------------------------------------


def insert_transition(
    tx: Tx, task_id: str, from_state: Optional[str], to_state: str, reason: Optional[str], actor: str, ts: str
) -> int:
    cur = tx.execute(
        "INSERT INTO state_transitions (task_id, from_state, to_state, reason, actor, ts) VALUES (?,?,?,?,?,?)",
        (task_id, from_state, to_state, reason, actor, ts),
    )
    return int(cur.lastrowid)


def last_transition(tx: Tx, task_id: str) -> Optional[StateTransition]:
    return StateTransition.from_row(
        tx.one("SELECT * FROM state_transitions WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,))
    )


def transitions(tx: Tx, task_id: str) -> list[StateTransition]:
    return [
        StateTransition.from_row(r)
        for r in tx.all("SELECT * FROM state_transitions WHERE task_id = ? ORDER BY id", (task_id,))
    ]
