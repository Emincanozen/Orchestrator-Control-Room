"""Projects repository."""

from __future__ import annotations

from typing import Any, Optional

from orchestrator.db import repo
from orchestrator.db.database import Tx
from orchestrator.domain.models import Project

JSON = ("verify_commands", "skill_profile")
UPDATABLE = ("name", "default_branch", "verify_commands", "skill_profile", "is_git", "isolation", "updated_at")


def insert(tx: Tx, values: dict[str, Any]) -> None:
    repo.insert(tx, "projects", values, JSON)


def get(tx: Tx, project_id: str) -> Optional[Project]:
    return Project.from_row(tx.one("SELECT * FROM projects WHERE id = ?", (project_id,)))


def get_by_path(tx: Tx, path: str) -> Optional[Project]:
    return Project.from_row(tx.one("SELECT * FROM projects WHERE path = ? COLLATE NOCASE", (path,)))


def list_all(tx: Tx) -> list[Project]:
    return [Project.from_row(r) for r in tx.all("SELECT * FROM projects ORDER BY created_at")]


def update(tx: Tx, project_id: str, fields: dict[str, Any]) -> int:
    return repo.update(tx, "projects", "id", project_id, fields, allowed=UPDATABLE, json_cols=JSON)


def delete(tx: Tx, project_id: str) -> int:
    return tx.execute("DELETE FROM projects WHERE id = ?", (project_id,)).rowcount
