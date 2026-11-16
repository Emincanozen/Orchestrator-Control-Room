"""Event bus: persist first (inside a DB transaction), broadcast after commit.

    bus.append(tx, type, ...)      # inside a caller's db.write(fn) transaction
    await bus.emit(type, ...)      # in its own transaction
    async with bus.subscribe() as sub: ev = await sub.get()
    await bus.replay(after_seq)

Payloads are always redacted before they are stored.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from orchestrator.db.database import Database, Tx
from orchestrator.domain.events import ALL_EVENT_TYPES, Event
from orchestrator.security.redact import redact_obj
from orchestrator.util import dumps, loads, now_iso

log = logging.getLogger(__name__)

MAX_PAYLOAD_CHARS = 64_000


class Subscription:
    def __init__(self, maxsize: int):
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self.loop = asyncio.get_running_loop()
        self.overflowed = False

    def _deliver(self, event: Event) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Slow consumer: stop feeding it. It must reconnect and replay by seq.
            self.overflowed = True

    async def get(self, timeout: Optional[float] = None) -> Event:
        if timeout is None:
            return await self.queue.get()
        return await asyncio.wait_for(self.queue.get(), timeout)


def event_from_row(row: Any) -> Event:
    data = dict(row)
    data["payload"] = loads(data.get("payload"), {}) or {}
    return Event.model_validate(data)


class EventBus:
    def __init__(self, db: Database):
        self.db = db
        self._subs: set[Subscription] = set()

    # ---- persist ---------------------------------------------------------
    def append(
        self,
        tx: Tx,
        type: str,
        *,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> Event:
        if type not in ALL_EVENT_TYPES:
            raise ValueError(f"unknown event type {type!r}")
        clean = redact_obj(payload or {})
        text = dumps(clean)
        if len(text) > MAX_PAYLOAD_CHARS:
            clean = {"truncated": True, "preview": text[: MAX_PAYLOAD_CHARS // 2]}
            text = dumps(clean)
        ts = now_iso()
        cur = tx.execute(
            "INSERT INTO events (ts, type, project_id, task_id, run_id, agent_id, payload) VALUES (?,?,?,?,?,?,?)",
            (ts, type, project_id, task_id, run_id, agent_id, text),
        )
        event = Event(
            seq=int(cur.lastrowid),
            ts=ts,
            type=type,
            project_id=project_id,
            task_id=task_id,
            run_id=run_id,
            agent_id=agent_id,
            payload=clean,
        )
        tx.after_commit(lambda: self.publish(event))
        return event

    async def emit(self, type: str, **kwargs: Any) -> Event:
        return await self.db.write(lambda tx: self.append(tx, type, **kwargs))

    # ---- broadcast -------------------------------------------------------
    def publish(self, event: Event) -> None:
        """Deliver a committed event to live subscribers. Never raises."""
        for sub in list(self._subs):
            try:
                sub.loop.call_soon_threadsafe(sub._deliver, event)
            except RuntimeError:  # subscriber loop closed
                self._subs.discard(sub)

    @asynccontextmanager
    async def subscribe(self, maxsize: int = 10_000) -> AsyncIterator[Subscription]:
        sub = Subscription(maxsize)
        self._subs.add(sub)
        try:
            yield sub
        finally:
            self._subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    # ---- replay ----------------------------------------------------------
    async def replay(
        self,
        after_seq: int = 0,
        *,
        limit: int = 1000,
        task_id: Optional[str] = None,
        types: Optional[list[str]] = None,
    ) -> list[Event]:
        limit = max(1, min(int(limit), 10_000))
        sql = "SELECT * FROM events WHERE seq > ?"
        params: list[Any] = [int(after_seq)]
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            params.extend(types)
        sql += " ORDER BY seq LIMIT ?"
        params.append(limit)
        rows = await self.db.all(sql, params)
        return [event_from_row(r) for r in rows]

    async def replay_window(self, after_seq: int, cap: int) -> tuple[int, int]:
        """(resume_after_seq, skipped): where to start a replay so that at most `cap` events are sent.
        When more than `cap` events are newer than `after_seq`, the replay starts at the newest `cap` events and
        `skipped` is the number of older ones left out; otherwise (after_seq, 0)."""
        after_seq = max(0, int(after_seq))
        cap = max(1, int(cap))
        row = await self.db.one("SELECT seq FROM events WHERE seq > ? ORDER BY seq DESC LIMIT 1 OFFSET ?", (after_seq, cap))
        if row is None:
            return after_seq, 0
        cut = int(row["seq"])
        n = await self.db.one("SELECT COUNT(*) AS n FROM events WHERE seq > ? AND seq <= ?", (after_seq, cut))
        return cut, int(n["n"]) if n else 0

    async def last_seq(self) -> int:
        row = await self.db.one("SELECT COALESCE(MAX(seq), 0) AS s FROM events")
        return int(row["s"]) if row else 0
