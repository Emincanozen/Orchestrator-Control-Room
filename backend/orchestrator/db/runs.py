"""Runs repository."""

from __future__ import annotations

from typing import Any, Optional

from orchestrator.db import repo
from orchestrator.db.database import Tx
from orchestrator.domain.models import Run

JSON = ("usage",)
UPDATABLE = (
    "model",
    "session_id",
    "pid",
    "status",
    "error_category",
    "error_message",
    "exit_code",
    "usage",
    "finished_at",
    "heartbeat_at",
    "agent_id",
)


def insert(tx: Tx, values: dict[str, Any]) -> None:
    repo.insert(tx, "runs", values, JSON)


def get(tx: Tx, run_id: str) -> Optional[Run]:
    return Run.from_row(tx.one("SELECT * FROM runs WHERE id = ?", (run_id,)))


def get_by_key(tx: Tx, key: str) -> Optional[Run]:
    return Run.from_row(tx.one("SELECT * FROM runs WHERE idempotency_key = ?", (key,)))


def for_task(tx: Tx, task_id: str, limit: int = 500) -> list[Run]:
    return [
        Run.from_row(r)
        for r in tx.all("SELECT * FROM runs WHERE task_id = ? ORDER BY started_at, id LIMIT ?", (task_id, limit))
    ]


def count_phase(tx: Tx, task_id: str, phase: str) -> int:
    return int(tx.scalar("SELECT COUNT(*) FROM runs WHERE task_id = ? AND phase = ?", (task_id, phase)) or 0)


def live(tx: Tx) -> list[Run]:
    return [Run.from_row(r) for r in tx.all("SELECT * FROM runs WHERE status IN ('starting','running') ORDER BY started_at")]


def update(tx: Tx, run_id: str, fields: dict[str, Any]) -> int:
    return repo.update(tx, "runs", "id", run_id, fields, allowed=UPDATABLE, json_cols=JSON)
