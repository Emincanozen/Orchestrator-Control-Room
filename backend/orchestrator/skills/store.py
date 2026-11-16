"""`skills` / `skill_events` persistence.

Extra data that has no column lives in the existing JSON columns:
- `install_method`: the catalog's install method (type/command/notes) + `catalog` (research extras: user_label,
  candidates, license, maintenance, default_branch, requires_credentials, paid_service, priority_candidate,
  duplicates_of, catalog_version) + `local` (runtime data owned by the manager, never overwritten by a re-import:
  source ("catalog"|"user"), original_source_url, archives[], last_error, incompatible[], pending_reactivation[]).
- `project_scopes`: the list of per-project activation records.
- `security`: the report (see static_inspect.py).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from orchestrator.db.database import Tx
from orchestrator.domain import events as ev
from orchestrator.services.context import AppContext
from orchestrator.util import dumps, new_id, now_iso

JSON_COLS = ("install_method", "compat", "dependencies", "permissions", "security", "project_scopes")
UPDATABLE = {
    "name", "category", "kind", "source_url", "source_verified", "uncertainty", "description", "install_method", "compat",
    "dependencies", "permissions", "security", "state", "pinned_ref", "installed_ref", "previous_ref", "local_path",
    "project_scopes", "updated_at",
}


def row_to_skill(row: Any) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    defaults = {"install_method": {}, "compat": {}, "dependencies": [], "permissions": [], "security": {}, "project_scopes": []}
    for col in JSON_COLS:
        raw = d.get(col)
        d[col] = json.loads(raw) if raw else defaults[col]
    d["source_verified"] = bool(d["source_verified"])
    im = d["install_method"]
    im.setdefault("catalog", {})
    im.setdefault("local", {})
    return d


def get_tx(tx: Tx, skill_id: str) -> Optional[dict[str, Any]]:
    return row_to_skill(tx.one("SELECT * FROM skills WHERE id = ?", (skill_id,)))


def list_tx(tx: Tx) -> list[dict[str, Any]]:
    return [row_to_skill(r) for r in tx.all("SELECT * FROM skills ORDER BY id")]  # type: ignore[misc]


def events_tx(tx: Tx, skill_id: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = tx.all("SELECT * FROM skill_events WHERE skill_id = ? ORDER BY ts DESC, id DESC LIMIT ?", (skill_id, limit))
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d["detail"]) if d.get("detail") else None
        except ValueError:
            pass
        out.append(d)
    return out


def insert_tx(tx: Tx, skill: dict[str, Any]) -> None:
    cols = list(skill)
    params = [dumps(skill[c]) if c in JSON_COLS else (int(skill[c]) if isinstance(skill[c], bool) else skill[c]) for c in cols]
    tx.execute(f"INSERT INTO skills ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", params)


def update_tx(tx: Tx, skill_id: str, fields: dict[str, Any]) -> None:
    bad = set(fields) - UPDATABLE
    if bad:
        raise ValueError(f"cannot update skills columns {sorted(bad)}")
    cols = list(fields)
    params = [dumps(fields[c]) if c in JSON_COLS else (int(fields[c]) if isinstance(fields[c], bool) else fields[c]) for c in cols]
    tx.execute(f"UPDATE skills SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?", [*params, skill_id])


def change_tx(
    ctx: AppContext,
    tx: Tx,
    skill_id: str,
    action: str,
    *,
    to_state: Optional[str] = None,
    fields: Optional[dict[str, Any]] = None,
    ref: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
    project_id: Optional[str] = None,
) -> dict[str, Any]:
    """Apply a change to a skill, write the `skill_events` audit row and emit `skill.state_changed`, all inside
    the caller's transaction (the bus broadcasts after commit). Returns the updated skill."""
    cur = get_tx(tx, skill_id)
    assert cur is not None, skill_id
    from_state = cur["state"]
    new_state = to_state or from_state
    upd = dict(fields or {})
    upd["state"] = new_state
    upd["updated_at"] = now_iso()
    update_tx(tx, skill_id, upd)
    det = dict(detail or {})
    if project_id:
        det.setdefault("project_id", project_id)
    tx.execute(
        "INSERT INTO skill_events (id, skill_id, action, from_state, to_state, ref, detail, ts) VALUES (?,?,?,?,?,?,?,?)",
        (new_id("sev"), skill_id, action, from_state, new_state, ref or upd.get("installed_ref") or cur.get("installed_ref"), dumps(det) if det else None, upd["updated_at"]),
    )
    ctx.bus.append(
        tx,
        ev.SKILL_STATE_CHANGED,
        project_id=project_id,
        payload={
            "skill_id": skill_id,
            "name": upd.get("name", cur["name"]),
            "action": action,
            "from_state": from_state,
            "to_state": new_state,
            "changed": from_state != new_state,
            "ref": ref or upd.get("installed_ref") or cur.get("installed_ref"),
            "project_id": project_id,
            "detail": det,
        },
    )
    out = get_tx(tx, skill_id)
    assert out is not None
    return out


def find_approval_tx(tx: Tx, kind: str, skill_id: str, ref: str, project_id: str, status: Optional[str] = None) -> Optional[Any]:
    """Latest approval of `kind` for (skill, ref, project)."""
    sql = (
        "SELECT * FROM approvals WHERE kind = ? AND json_extract(details,'$.skill_id') = ? "
        "AND json_extract(details,'$.ref') = ? AND json_extract(details,'$.project_id') = ?"
    )
    params: list[Any] = [kind, skill_id, ref, project_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY requested_at DESC LIMIT 1"
    return tx.one(sql, params)


def has_approved_any_project_tx(tx: Tx, kind: str, skill_id: str, ref: str) -> bool:
    return bool(
        tx.scalar(
            "SELECT 1 FROM approvals WHERE kind = ? AND status = 'approved' AND json_extract(details,'$.skill_id') = ? "
            "AND json_extract(details,'$.ref') = ? LIMIT 1",
            (kind, skill_id, ref),
        )
    )
