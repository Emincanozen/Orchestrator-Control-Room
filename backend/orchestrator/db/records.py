"""Repositories for small record tables: agents, approvals, checkpoints,
verifications, settings, routing decisions."""

from __future__ import annotations

from typing import Any, Optional

from orchestrator.db import repo
from orchestrator.db.database import Tx
from orchestrator.domain.models import Agent, Approval, Checkpoint, RoutingDecision, Verification
from orchestrator.util import dumps, loads, now_iso

# ---- agents ----------------------------------------------------------------

AGENT_UPDATABLE = (
    "provider",
    "model",
    "activity",
    "current_task_id",
    "current_run_id",
    "current_operation",
    "since",
)


def agent_insert(tx: Tx, values: dict[str, Any]) -> None:
    repo.insert(tx, "agents", values)


def agent_get(tx: Tx, agent_id: str) -> Optional[Agent]:
    return Agent.from_row(tx.one("SELECT * FROM agents WHERE id = ?", (agent_id,)))


def agent_list(tx: Tx) -> list[Agent]:
    return [Agent.from_row(r) for r in tx.all("SELECT * FROM agents ORDER BY seat_order, id")]


def agent_update(tx: Tx, agent_id: str, fields: dict[str, Any]) -> int:
    return repo.update(tx, "agents", "id", agent_id, fields, allowed=AGENT_UPDATABLE)


def agent_count(tx: Tx) -> int:
    return int(tx.scalar("SELECT COUNT(*) FROM agents") or 0)


# ---- approvals -------------------------------------------------------------


def approval_insert(tx: Tx, values: dict[str, Any]) -> None:
    repo.insert(tx, "approvals", values, ("details",))


def approval_get(tx: Tx, approval_id: str) -> Optional[Approval]:
    return Approval.from_row(tx.one("SELECT * FROM approvals WHERE id = ?", (approval_id,)))


def approval_list(tx: Tx, status: Optional[str] = None, task_id: Optional[str] = None) -> list[Approval]:
    sql, params = "SELECT * FROM approvals WHERE 1=1", []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if task_id:
        sql += " AND task_id = ?"
        params.append(task_id)
    sql += " ORDER BY requested_at DESC LIMIT 1000"
    return [Approval.from_row(r) for r in tx.all(sql, params)]


def approval_update(tx: Tx, approval_id: str, fields: dict[str, Any]) -> int:
    return repo.update(
        tx, "approvals", "id", approval_id, fields, allowed=("status", "resolved_at", "resolved_by", "note")
    )


# ---- checkpoints -----------------------------------------------------------


def checkpoint_insert(tx: Tx, values: dict[str, Any]) -> None:
    repo.insert(tx, "checkpoints", values, ("summary",))


def checkpoint_get(tx: Tx, checkpoint_id: str) -> Optional[Checkpoint]:
    return Checkpoint.from_row(tx.one("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)))


def checkpoints_for_task(tx: Tx, task_id: str) -> list[Checkpoint]:
    return [
        Checkpoint.from_row(r)
        for r in tx.all("SELECT * FROM checkpoints WHERE task_id = ? ORDER BY created_at, id", (task_id,))
    ]


# ---- verifications ---------------------------------------------------------


def verification_insert(tx: Tx, values: dict[str, Any]) -> None:
    repo.insert(tx, "verifications", values, ("checks",))


def verifications_for_task(tx: Tx, task_id: str) -> list[Verification]:
    return [
        Verification.from_row(r)
        for r in tx.all("SELECT * FROM verifications WHERE task_id = ? ORDER BY rowid", (task_id,))
    ]


def latest_verification(tx: Tx, task_id: str) -> Optional[Verification]:
    return Verification.from_row(
        tx.one("SELECT * FROM verifications WHERE task_id = ? ORDER BY rowid DESC LIMIT 1", (task_id,))
    )


def has_accepting_verification(tx: Tx, task_id: str) -> bool:
    """True if the most recent verification record allows completion (ADR-007)
    and no execute/repair run started after it (stale evidence does not count)."""
    v = latest_verification(tx, task_id)
    if v is None or v.status not in ("passed", "unverifiable_accepted"):
        return False
    newer = tx.scalar(
        "SELECT COUNT(*) FROM runs WHERE task_id = ? AND phase IN ('execute','repair') AND started_at > ?",
        (task_id, v.started_at),
    )
    return not newer


# ---- settings --------------------------------------------------------------


def settings_all(tx: Tx) -> dict[str, Any]:
    return {r["key"]: loads(r["value"]) for r in tx.all("SELECT key, value FROM settings ORDER BY key")}


def setting_put(tx: Tx, key: str, value: Any) -> None:
    tx.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, dumps(value), now_iso()),
    )


def setting_delete(tx: Tx, key: str) -> None:
    tx.execute("DELETE FROM settings WHERE key = ?", (key,))


# ---- routing decisions -----------------------------------------------------


def routing_insert(tx: Tx, values: dict[str, Any]) -> None:
    repo.insert(tx, "routing_decisions", values, ("candidates", "reasons", "rule_ids", "detail"))


def routing_for_task(tx: Tx, task_id: str) -> list[RoutingDecision]:
    return [
        RoutingDecision.from_row(r)
        for r in tx.all("SELECT * FROM routing_decisions WHERE task_id = ? ORDER BY created_at", (task_id,))
    ]
