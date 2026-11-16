"""Events REST + WebSocket `/ws?after_seq=`: replay seq > N, then live.

Authentication of the WebSocket (either works; the header keeps the token out of URLs and access logs):
  * `Sec-WebSocket-Protocol: orch, orch.token.<token>` (browser: `new WebSocket(url, ["orch", "orch.token." + token])`)
    -> the server accepts and echoes the subprotocol `orch`;
  * `?token=<token>` query parameter (kept for backwards compatibility; uvicorn log lines are redacted).

Replay cap: at most `limits.ws_replay_max_events` (default 2000) events are replayed per connection. When the
client is further behind, the replay starts at the newest events and the server first sends
    {"type": "replay.truncated", "requested_after_seq": N, "resume_after_seq": M, "skipped": K, "cap": C, "seq": M}
(`seq` is NOT an event seq, like every control frame) and `replay.done` carries `"truncated": true`. The client
should reconcile its state over REST (the dashboard already does when replay.done.seq > its requested seq).

Ordering guarantee: the client subscribes before replay, so no event committed
after the replay query can be missed; duplicates are dropped by seq.
Slow clients whose queue overflows are closed with 4408 and must reconnect
with their last seq.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from orchestrator.api.deps import AUTH, get_rt
from orchestrator.api.runtime import Runtime
from orchestrator.db.retention import DEFAULT_WS_REPLAY_MAX, limit_value
from orchestrator.domain.events import Event
from orchestrator.security.auth import token_matches

router = APIRouter(prefix="/api", tags=["events"], dependencies=AUTH)
ws_router = APIRouter(tags=["events"])

REPLAY_PAGE = 500
WS_REPLAY_KEY = "ws_replay_max_events"
KEEPALIVE_SECONDS = 25.0


@router.get("/events")
async def list_events(
    after_seq: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
    task_id: Optional[str] = None,
    rt: Runtime = Depends(get_rt),
) -> list[Event]:
    return await rt.ctx.bus.replay(after_seq, limit=limit, task_id=task_id)


SUBPROTOCOL = "orch"
TOKEN_SUBPROTOCOL_PREFIX = "orch.token."


def _subprotocol_token(websocket: WebSocket) -> tuple[Optional[str], Optional[str]]:
    """(token, offered protocol carrying it) from the offered subprotocols, if any."""
    for proto in websocket.scope.get("subprotocols") or []:
        if isinstance(proto, str) and proto.startswith(TOKEN_SUBPROTOCOL_PREFIX) and len(proto) > len(TOKEN_SUBPROTOCOL_PREFIX):
            return proto[len(TOKEN_SUBPROTOCOL_PREFIX):], proto
    return None, None


@ws_router.websocket("/ws")
async def events_ws(websocket: WebSocket, token: Optional[str] = None, after_seq: int = 0) -> None:
    rt: Runtime = websocket.app.state.rt
    proto_token, proto = _subprotocol_token(websocket)
    if not (token_matches(rt.token, proto_token) or token_matches(rt.token, token)):
        await websocket.close(code=4401, reason="invalid token")
        return
    offered = websocket.scope.get("subprotocols") or []
    # A browser fails the handshake if the server selects a protocol the client did not offer.
    selected = SUBPROTOCOL if SUBPROTOCOL in offered else proto
    await websocket.accept(subprotocol=selected)
    bus = rt.ctx.bus
    last = max(0, int(after_seq))
    disconnected = asyncio.Event()

    async def reader() -> None:
        try:
            while True:  # ends when the client disconnects
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            disconnected.set()

    reader_task = asyncio.create_task(reader())
    try:
        cap = max(1, limit_value(await rt.ctx.policies(), WS_REPLAY_KEY, DEFAULT_WS_REPLAY_MAX))
        async with bus.subscribe() as sub:
            requested = last
            last, skipped = await bus.replay_window(last, cap)
            truncated = skipped > 0
            if truncated:
                await websocket.send_json({"type": "replay.truncated", "requested_after_seq": requested, "resume_after_seq": last,
                                           "skipped": skipped, "cap": cap, "seq": last})
            sent = 0
            while sent < cap:
                want = min(REPLAY_PAGE, cap - sent)
                batch = await bus.replay(last, limit=want)
                for e in batch:
                    await websocket.send_json(e.model_dump(mode="json"))
                    last = e.seq
                sent += len(batch)
                if len(batch) < want:
                    break
            done: dict = {"type": "replay.done", "seq": last}
            if truncated:
                done["truncated"] = True
            await websocket.send_json(done)
            while not disconnected.is_set():  # connection lifetime
                getter = asyncio.ensure_future(sub.get())
                stopper = asyncio.ensure_future(disconnected.wait())
                done, _ = await asyncio.wait({getter, stopper}, timeout=KEEPALIVE_SECONDS, return_when=asyncio.FIRST_COMPLETED)
                stopper.cancel()
                if getter not in done:
                    getter.cancel()
                    if not done:
                        await websocket.send_json({"type": "ping", "seq": last})
                    continue
                if sub.overflowed:
                    await websocket.close(code=4408, reason="client too slow; reconnect with after_seq")
                    return
                e = getter.result()
                if e.seq <= last:
                    continue
                await websocket.send_json(e.model_dump(mode="json"))
                last = e.seq
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        reader_task.cancel()
