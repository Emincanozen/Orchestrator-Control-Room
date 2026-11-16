"""Bounded retention for the `events` table (SECURITY_REVIEW #15).

Rules (all of them must hold for an event to be deleted):
  * it is older than `limits.events_max_age_days` OR it is beyond the newest `limits.events_max_rows` rows;
  * it is not the newest event (`MAX(seq)` stays so `last_seq`/resume points never go backwards);
  * its task is not live: events of tasks that are not COMPLETED/CANCELLED are kept, because recovery, the
    handoff builder and the dashboard timeline read them (events without a task are fair game).
`seq` is AUTOINCREMENT, so deleted numbers are never reused. Deletion runs in small batches, each its own short
write transaction, so a large backlog never holds the writer lock for long.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from orchestrator.db.database import Database
from orchestrator.domain.states import TERMINAL_STATES
from orchestrator.util import to_iso, utcnow

log = logging.getLogger(__name__)

DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_MAX_ROWS = 200_000
DEFAULT_WS_REPLAY_MAX = 2_000
BATCH = 5_000


def limit_value(policies: Any, key: str, default: int) -> int:
    """`limits.<key>` from the effective policies with a code-level default (the YAML need not list it)."""
    try:
        raw = (policies.data.get("limits") or {}).get(key, default)
        value = int(raw)
    except (AttributeError, TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _cutoff_iso(days: int) -> str:
    return to_iso(utcnow() - timedelta(days=days))


def _prune_batch(tx: Any, cutoff: Optional[str], row_cut: int, batch: int) -> int:
    terminal = sorted(s.value for s in TERMINAL_STATES)
    marks = ",".join("?" * len(terminal))
    conds, params = [], []
    if cutoff is not None:
        conds.append("ts < ?")
        params.append(cutoff)
    if row_cut > 0:
        conds.append("seq <= ?")
        params.append(row_cut)
    if not conds:
        return 0
    # `tasks` rows in a terminal state are the only ones whose events may go; events of unknown/NULL tasks may too.
    sql = (
        "DELETE FROM events WHERE seq IN ("
        f"SELECT seq FROM events WHERE ({' OR '.join(conds)}) "
        "AND seq < (SELECT COALESCE(MAX(seq), 0) FROM events) "
        f"AND (task_id IS NULL OR task_id NOT IN (SELECT id FROM tasks WHERE state NOT IN ({marks}))) "
        "ORDER BY seq LIMIT ?)"
    )
    cur = tx.execute(sql, [*params, *terminal, batch])
    return int(cur.rowcount or 0)


async def prune_events(db: Database, *, max_age_days: int = DEFAULT_MAX_AGE_DAYS, max_rows: int = DEFAULT_MAX_ROWS, batch: int = BATCH) -> int:
    """Delete events per the rules above. Returns the number of rows removed. `0` disables the matching limit."""
    cutoff = _cutoff_iso(max_age_days) if max_age_days > 0 else None
    total = 0
    while True:
        max_seq = await db.read(lambda tx: int(tx.scalar("SELECT COALESCE(MAX(seq), 0) FROM events") or 0))
        row_cut = max_seq - max_rows if max_rows > 0 else 0
        removed = await db.write(lambda tx: _prune_batch(tx, cutoff, row_cut, batch))
        total += removed
        if removed < batch:
            break
    if total:
        log.info("events retention removed %d rows (max_age_days=%s, max_rows=%s)", total, max_age_days, max_rows)
    return total
