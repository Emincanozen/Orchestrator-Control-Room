"""Event rendering and live following of a task (WebSocket first, REST polling fallback)."""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from orchestrator.cli.client import Api, CliError, client_injected
from orchestrator.cli.output import short

# Following ends in one of these states. Only COMPLETED is a success.
STOP_STATES = {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED", "PAUSED"}
FAILURE_STATES = {"FAILED", "CANCELLED", "BLOCKED", "PAUSED"}

# Event types shown by `logs` / `run --follow` (the rest, e.g. run.usage, is noise unless --raw).
LOG_TYPES = {
    "run.output", "run.tool_use", "run.started", "run.finished", "task.state_changed", "verification.result",
    "approval.requested", "approval.resolved", "router.decision", "checkpoint.created", "system.notice",
    "advisor.consulted", "decision.recorded",
}


def stamp(ts: Any) -> str:
    s = str(ts or "")
    return s[:19].replace("T", " ") if len(s) >= 19 else s


def format_event(e: dict[str, Any], *, raw: bool = False) -> list[str]:
    """Human lines for one event (several for multi-line output). `raw` -> one JSON line, nothing dropped."""
    if raw:
        return [json.dumps(e, ensure_ascii=True, default=str)]
    t = e.get("type")
    p = e.get("payload") or {}
    pre = f"{stamp(e.get('ts'))}  "
    if t == "run.output":
        text = str(p.get("text", ""))
        lines = text.splitlines() or [""]
        return [f"{pre}| {ln}" for ln in lines]
    if t == "run.tool_use":
        name = p.get("name") or p.get("tool") or "tool"
        args = {k: v for k, v in p.items() if k not in ("name", "tool")}
        detail = short(json.dumps(args, ensure_ascii=True, default=str), 160) if args else ""
        return [f"{pre}tool: {name} {detail}".rstrip()]
    if t == "task.state_changed":
        reason = f" ({p.get('reason')})" if p.get("reason") else ""
        return [f"{pre}state: {p.get('from')} -> {p.get('to')}{reason}"]
    if t == "verification.result":
        checks = p.get("checks") or []
        passed = sum(1 for c in checks if c.get("passed"))
        lines = [f"{pre}verification: {p.get('status')} ({passed}/{len(checks)} checks passed)"]
        for c in checks:
            mark = "ok  " if c.get("passed") else "FAIL"
            extra = f" - {c.get('error')}" if c.get("error") else ""
            lines.append(f"{pre}  [{mark}] {c.get('name')}{extra}")
        return lines
    if t == "run.started":
        return [f"{pre}run started: {p.get('provider', '?')}/{p.get('model') or '?'} phase={p.get('phase', '?')}"]
    if t == "run.finished":
        return [f"{pre}run finished: {p.get('status', '?')}" + (f" - {p.get('error')}" if p.get("error") else "")]
    if t == "approval.requested":
        a = p.get("approval") or {}
        return [f"{pre}approval requested: {a.get('summary') or a.get('kind') or ''} (id {a.get('id') or '?'})"]
    if t == "approval.resolved":
        a = p.get("approval") or {}
        return [f"{pre}approval resolved: {a.get('status') or p.get('status') or ''} (id {a.get('id') or '?'})"]
    return [f"{pre}{t}: {short(json.dumps(p, ensure_ascii=True, default=str), 160)}"]


WS_SUBPROTOCOLS_PREFIX = "orch.token."


def _ws_subprotocols(token: str) -> list[str]:
    """The token travels in the Sec-WebSocket-Protocol header, never in the URL (access logs, history)."""
    return ["orch", WS_SUBPROTOCOLS_PREFIX + token]


def _ws_url(base: str, after_seq: int) -> str:
    parts = urlsplit(base)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, "/ws", f"after_seq={after_seq}", ""))


def stream_via_ws(api: Api, task_id: str, after_seq: int, emit: Callable[[dict[str, Any]], None],
                  task_state: Callable[[], str]) -> tuple[int, bool]:
    """Follow over `/ws`. Returns (last_seq_seen, finished). Raises on any transport problem so the caller can fall back."""
    from websockets.sync.client import connect  # imported lazily: optional at runtime

    last = after_seq
    url = _ws_url(api.base_url, after_seq)
    with connect(url, subprotocols=_ws_subprotocols(api.token()), open_timeout=5, max_size=8 * 1024 * 1024, proxy=None) as ws:
        while True:
            try:
                frame = ws.recv(timeout=20)
            except TimeoutError:
                if task_state() in STOP_STATES:  # the server only pings every 25 s: do not wait for it
                    return last, True
                continue
            msg = json.loads(frame)
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            if kind in ("replay.done", "ping") and "seq" in msg and "ts" not in msg:
                if kind == "replay.done" and task_state() in STOP_STATES:
                    return last, True  # replay already contained the end state
                continue
            seq = msg.get("seq")
            if isinstance(seq, int):
                last = max(last, seq)
            if msg.get("task_id") != task_id:
                continue
            emit(msg)
            if kind == "task.state_changed" and (msg.get("payload") or {}).get("to") in STOP_STATES:
                return last, True


def stream_via_polling(api: Api, task_id: str, after_seq: int, emit: Callable[[dict[str, Any]], None],
                       task_state: Callable[[], str], interval: float = 0.5, deadline: Optional[float] = None) -> int:
    last = after_seq
    while True:
        batch = api.get("/api/events", after_seq=last, task_id=task_id, limit=500) or []
        for e in batch:
            last = max(last, int(e.get("seq", last)))
            emit(e)
        if len(batch) >= 500:
            continue
        # Read the state AFTER the events: if it is final and no event is left, we are done.
        if task_state() in STOP_STATES:
            tail = api.get("/api/events", after_seq=last, task_id=task_id, limit=500) or []
            if not tail:
                return last
            continue
        if deadline is not None and time.monotonic() > deadline:
            return last
        time.sleep(interval)


def follow(api: Api, task_id: str, after_seq: int, emit: Callable[[dict[str, Any]], None], *,
           prefer_ws: bool = True) -> dict[str, Any]:
    """Emit the task's events from `after_seq` until it reaches a stop state; return the final task JSON."""

    def state() -> str:
        t = api.get(f"/api/tasks/{task_id}")
        return str((t or {}).get("state"))

    last = after_seq
    done = False
    if prefer_ws and not client_injected():
        try:
            import websockets  # noqa: F401
        except ImportError:
            prefer_ws = False
        else:
            seen = {"last": after_seq}

            def tracking(e: dict[str, Any]) -> None:
                seen["last"] = max(seen["last"], int(e.get("seq", 0)))
                emit(e)

            try:
                last, done = stream_via_ws(api, task_id, after_seq, tracking, state)
            except CliError:
                raise
            except Exception:  # noqa: BLE001 - any WS problem (handshake 403, 4403, reset, ...) -> polling
                last = max(last, seen["last"])
    if not done:
        stream_via_polling(api, task_id, last, emit, state)
    return api.get(f"/api/tasks/{task_id}")
