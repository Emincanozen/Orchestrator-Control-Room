"""Repositories for provider telemetry: provider_status, provider_models, usage_snapshots,
routing history reads and the adaptive-routing history statistics."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from orchestrator.db import repo
from orchestrator.db.database import Tx
from orchestrator.domain.models import ProviderModelRow, ProviderStatusRow, RoutingDecision, UsageSnapshot
from orchestrator.util import dumps

# ---- provider_status -------------------------------------------------------


def status_upsert(tx: Tx, values: dict[str, Any]) -> None:
    cols = ["provider", "installed", "version", "authenticated", "executable", "capabilities", "checked_at", "error"]
    row = {c: values.get(c) for c in cols}
    row["installed"] = int(bool(row["installed"]))
    if row["authenticated"] is not None:
        row["authenticated"] = int(bool(row["authenticated"]))
    row["capabilities"] = dumps(row["capabilities"] or {})
    tx.execute(
        f"INSERT INTO provider_status ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        "ON CONFLICT(provider) DO UPDATE SET "
        + ", ".join(f"{c} = excluded.{c}" for c in cols if c != "provider"),
        [row[c] for c in cols],
    )


def status_list(tx: Tx) -> list[ProviderStatusRow]:
    return [ProviderStatusRow.from_row(r) for r in tx.all("SELECT * FROM provider_status ORDER BY provider")]


# ---- provider_models -------------------------------------------------------


def models_replace(tx: Tx, provider: str, rows: Iterable[dict[str, Any]]) -> None:
    tx.execute("DELETE FROM provider_models WHERE provider = ?", (provider,))
    for r in rows:
        repo.insert(tx, "provider_models", {**r, "provider": provider}, ("capabilities",))


def models_list(tx: Tx) -> list[ProviderModelRow]:
    return [ProviderModelRow.from_row(r) for r in tx.all("SELECT * FROM provider_models ORDER BY provider, model_id")]


# ---- usage_snapshots -------------------------------------------------------


def snapshot_insert(tx: Tx, values: dict[str, Any]) -> None:
    repo.insert(tx, "usage_snapshots", values)


def snapshots_latest(tx: Tx) -> list[UsageSnapshot]:
    """Newest row per (provider, model, metric). Enough input for derive_state: it only ever
    looks at the latest observation per metric plus the latest success / auth-ok marker."""
    rows = tx.all(
        """
        SELECT * FROM (
            SELECT s.*, ROW_NUMBER() OVER (
                PARTITION BY provider, COALESCE(model, ''), metric ORDER BY observed_at DESC, rowid DESC
            ) AS rn
            FROM usage_snapshots s
        ) WHERE rn = 1 ORDER BY provider, model, metric
        """
    )
    return [UsageSnapshot.from_row({k: r[k] for k in r.keys() if k != "rn"}) for r in rows]


# ---- routing history -------------------------------------------------------


def routing_recent(tx: Tx, task_id: Optional[str] = None, limit: int = 50) -> list[RoutingDecision]:
    sql, params = "SELECT * FROM routing_decisions", []
    if task_id:
        sql += " WHERE task_id = ?"
        params.append(task_id)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    return [RoutingDecision.from_row(r) for r in tx.all(sql, params)]


def history_stats(tx: Tx) -> dict[tuple[str, str, str], dict[str, int]]:
    """Adaptive-routing evidence per (provider, model, task_type).

    DEFINITION (explicit, see RoutingService): a run counts as a *verified run* when
      * it is an execute/repair run that ended `succeeded` or `failed`
        (cancelled / interrupted runs say nothing about the model), and
      * its task LATER reached a passing verification (a `verifications` row with status
        'passed' whose started_at >= the run's started_at).
    `successes` = verified runs whose own status is `succeeded`; a failed run on a task that was
    later completed by another run counts as a verified run but not as a success.
    Tasks that never passed verification contribute nothing (their outcome is unproven).
    """
    rows = tx.all(
        """
        SELECT r.provider AS provider, r.model AS model, t.task_type AS task_type,
               COUNT(*) AS verified_runs,
               SUM(CASE WHEN r.status = 'succeeded' THEN 1 ELSE 0 END) AS successes
        FROM runs r JOIN tasks t ON t.id = r.task_id
        WHERE r.phase IN ('execute', 'repair') AND r.status IN ('succeeded', 'failed') AND r.model IS NOT NULL
          AND EXISTS (SELECT 1 FROM verifications v
                      WHERE v.task_id = r.task_id AND v.status = 'passed' AND v.started_at >= r.started_at)
        GROUP BY r.provider, r.model, t.task_type
        """
    )
    return {
        (r["provider"], r["model"], r["task_type"]): {"verified_runs": int(r["verified_runs"]), "successes": int(r["successes"] or 0)}
        for r in rows
    }
