"""SECURITY_REVIEW #15: bounded events table and a capped WebSocket replay."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.db import records
from orchestrator.db.retention import DEFAULT_MAX_AGE_DAYS, DEFAULT_MAX_ROWS, DEFAULT_WS_REPLAY_MAX, limit_value, prune_events
from orchestrator.domain.states import TaskState
from orchestrator.providers.mock import MockProvider
from orchestrator.testing import make_harness
from orchestrator.util import to_iso, utcnow

BASE = "http://127.0.0.1:8765"


@pytest.fixture
async def h(tmp_path):
    harness = await make_harness(tmp_path)
    try:
        yield harness
    finally:
        await harness.close()


def _insert(tx, n: int, *, ts: str, task_id=None, type_="agent.status"):
    for _ in range(n):
        tx.execute("INSERT INTO events (ts, type, task_id, payload) VALUES (?,?,?,'{}')", (ts, type_, task_id))


async def count(h) -> int:
    return int(await h.ctx.db.read(lambda tx: tx.scalar("SELECT COUNT(*) FROM events")))


def test_defaults():
    class P:
        data = {}

    assert (limit_value(P, "events_max_age_days", DEFAULT_MAX_AGE_DAYS), limit_value(P, "events_max_rows", DEFAULT_MAX_ROWS)) == (30, 200_000)
    assert DEFAULT_WS_REPLAY_MAX == 2000

    class Q:
        data = {"limits": {"events_max_rows": "50", "events_max_age_days": -3}}

    assert limit_value(Q, "events_max_rows", 1) == 50 and limit_value(Q, "events_max_age_days", 30) == 30


async def test_age_retention_removes_old_events_but_keeps_newest_and_live_tasks(h):
    proj = await h.project("p", git_repo=False)
    live = await h.task(proj.id)   # CREATED/QUEUED: not terminal
    done = await h.task(proj.id)
    await h.ctx.db.write(lambda tx: tx.execute("UPDATE tasks SET state = ? WHERE id = ?", (TaskState.COMPLETED.value, done.id)))
    old = to_iso(utcnow() - timedelta(days=90))
    await h.ctx.db.write(lambda tx: (_insert(tx, 5, ts=old), _insert(tx, 3, ts=old, task_id=done.id), _insert(tx, 4, ts=old, task_id=live.id)))
    before = await count(h)
    last = await h.ctx.bus.last_seq()
    removed = await prune_events(h.ctx.db, max_age_days=30, max_rows=0, batch=3)  # tiny batch: several rounds
    assert removed == 8  # 5 task-less + 3 of the completed task
    assert await count(h) == before - 8
    kept = await h.ctx.db.all("SELECT task_id FROM events WHERE ts = ?", (old,))
    assert [r["task_id"] for r in kept] == [live.id] * 4  # the live task's history survives
    assert await h.ctx.bus.last_seq() == last


async def test_row_cap_keeps_newest_rows_and_seq_never_goes_back(h):
    ts = to_iso(utcnow())
    await h.ctx.db.write(lambda tx: _insert(tx, 100, ts=ts))
    last = await h.ctx.bus.last_seq()
    await prune_events(h.ctx.db, max_age_days=0, max_rows=10)
    seqs = [r["seq"] for r in await h.ctx.db.all("SELECT seq FROM events ORDER BY seq")]
    assert len(seqs) == 10 and seqs[-1] == last and seqs == list(range(last - 9, last + 1))
    from orchestrator.domain.events import ALL_EVENT_TYPES
    e = await h.ctx.bus.emit(sorted(ALL_EVENT_TYPES)[0], payload={})
    assert e.seq == last + 1  # AUTOINCREMENT: numbers of deleted rows are never reused


async def test_retention_never_deletes_the_only_event_even_if_old(h):
    old = to_iso(utcnow() - timedelta(days=400))
    await h.ctx.db.write(lambda tx: tx.execute("DELETE FROM events"))
    await h.ctx.db.write(lambda tx: _insert(tx, 3, ts=old))
    await prune_events(h.ctx.db, max_age_days=30, max_rows=0)
    assert await count(h) == 1


@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=tmp_path / "data", providers={"mock": MockProvider()}, start_scheduler=False)
    with TestClient(app, base_url=BASE) as c:
        c.token = app.state.rt.token
        c.rt = app.state.rt
        yield c


def _ws(c, after_seq: int):
    return c.websocket_connect(f"ws://127.0.0.1:8765/ws?token={c.token}&after_seq={after_seq}")


def _drain(ws):
    frames = []
    while True:
        m = ws.receive_json()
        frames.append(m)
        if m.get("type") == "replay.done":
            return frames


def test_ws_replay_is_capped_and_reports_truncation(client):
    rt = client.rt
    ts = to_iso(utcnow())
    rt.ctx.db.write_sync(lambda tx: _insert(tx, 120, ts=ts))
    rt.ctx.db.write_sync(lambda tx: records.setting_put(tx, "limits", {"ws_replay_max_events": 50}))
    with _ws(client, 0) as ws:
        frames = _drain(ws)
    events = [f for f in frames if "ts" in f]
    done = frames[-1]
    assert len(events) == 50 and events[-1]["seq"] == done["seq"]  # the newest 50, contiguous
    assert [e["seq"] for e in events] == list(range(done["seq"] - 49, done["seq"] + 1))
    trunc = [f for f in frames if f.get("type") == "replay.truncated"]
    assert len(trunc) == 1 and frames[0] is trunc[0]
    t = trunc[0]
    assert t["requested_after_seq"] == 0 and t["cap"] == 50 and t["resume_after_seq"] == events[0]["seq"] - 1
    assert t["skipped"] == t["resume_after_seq"] and t["skipped"] >= 70 and t["seq"] == t["resume_after_seq"]
    assert done["type"] == "replay.done" and done["truncated"] is True


def test_ws_replay_within_cap_is_unchanged(client):
    rt = client.rt
    ts = to_iso(utcnow())
    last0 = rt.ctx.db.read_sync(lambda tx: tx.scalar("SELECT COALESCE(MAX(seq),0) FROM events"))
    rt.ctx.db.write_sync(lambda tx: _insert(tx, 7, ts=ts))
    with _ws(client, last0) as ws:
        frames = _drain(ws)
    seqs = [f["seq"] for f in frames if "ts" in f]
    assert len(seqs) >= 7 and seqs == list(range(last0 + 1, last0 + 1 + len(seqs)))
    assert not any(f.get("type") == "replay.truncated" for f in frames)
    assert "truncated" not in frames[-1] and frames[-1]["type"] == "replay.done"
