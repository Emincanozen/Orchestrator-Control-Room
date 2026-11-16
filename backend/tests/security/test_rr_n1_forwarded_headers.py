"""Re-review N1 (HIGH): `X-Forwarded-For` / `Forwarded` must not let an agent worker pass the worker-origin guard.

uvicorn's defaults (`proxy_headers=True`, `forwarded_allow_ips="127.0.0.1"`) let any loopback client rewrite
`scope["client"]`. Reproduced by the reviewer: a job-confined child got 403 for `PUT /api/settings` without the
header and 200 with `X-Forwarded-For: 8.8.8.8` or `testclient`.

Real uvicorn (its default config AND the config `orchestrator serve` uses), the real app, real child processes
spawned through the provider spawn path (so they are in the worker job)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import textwrap
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from orchestrator.api import security as api_security
from orchestrator.api.app import create_app
from orchestrator.api.security import PeerVerdict, WorkerOriginMiddleware, classify_peer
from orchestrator.providers.mock import MockProvider
from orchestrator.providers.process import spawn_process

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows-only")

BODY = {"values": {"limits": {"retry_backoff_seconds": 0}}}
VARIANTS = [
    None,
    ("X-Forwarded-For", "8.8.8.8"),
    ("X-Forwarded-For", "testclient"),
    ("X-Forwarded-For", "::1"),
    ("X-Forwarded-For", "127.0.0.1"),
    ("X-Forwarded-For", "8.8.8.8:4444, 127.0.0.1"),
    ("Forwarded", "for=8.8.8.8"),
    ("Forwarded", 'for="[::1]:1234"'),
    ("X-Real-IP", "8.8.8.8"),
]

# Child: one PUT per header variant; prints one JSON line per request.
CLIENT = textwrap.dedent(
    """
    import json, sys, urllib.request, urllib.error
    port, token, variants, body = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), sys.argv[4].encode()
    for v in variants:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/settings", method="PUT", data=body,
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        if v:
            req.add_header(v[0], v[1])
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                print(json.dumps({"v": v, "status": r.status, "code": None}), flush=True)
        except urllib.error.HTTPError as e:
            code = (json.loads(e.read() or b"{}").get("error") or {}).get("code")
            print(json.dumps({"v": v, "status": e.code, "code": code}), flush=True)
    """
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cli_uvicorn_kwargs(monkeypatch, tmp_path: Path) -> dict:
    """The kwargs `orchestrator serve` passes to uvicorn.run (captured, nothing is served)."""
    import uvicorn
    from typer.testing import CliRunner

    from orchestrator.cli import main as cli_main

    captured: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: captured.update(kw))
    monkeypatch.setattr(cli_main, "_probe_instance_lock", lambda: None)
    monkeypatch.setenv("ORCHESTRATOR_DATA_DIR", str(tmp_path / "cli-data"))
    result = CliRunner().invoke(cli_main.app, ["serve", "--no-browser"])
    assert result.exit_code == 0, result.output
    return captured


def _serve(app, port: int, **extra):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on", **extra))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        assert thread.is_alive(), "server thread died"
        time.sleep(0.05)
    return server, thread


@pytest.fixture(params=["uvicorn-default", "orchestrator-serve"])
def live(request, tmp_path: Path, monkeypatch):
    extra: dict = {}
    if request.param == "orchestrator-serve":
        kw = _cli_uvicorn_kwargs(monkeypatch, tmp_path)
        assert kw["proxy_headers"] is False and kw["forwarded_allow_ips"] == ""
        extra = {"proxy_headers": kw["proxy_headers"], "forwarded_allow_ips": kw["forwarded_allow_ips"]}
    app = create_app(data_dir=tmp_path / "data", providers={"mock": MockProvider()}, start_scheduler=False,
                     trust_test_client=False)
    port = _free_port()
    server, thread = _serve(app, port, **extra)
    try:
        yield request.param, port, app.state.rt.token
    finally:
        server.should_exit = True
        thread.join(15)
        api_security.note_proxy_headers_enabled(False)


async def _run_worker(args: list[str]) -> list[str]:
    handle = await spawn_process([sys.executable, *args], cwd=os.getcwd(), env=dict(os.environ), timeout=90)
    lines = [line async for kind, line in handle.stream() if kind == "stdout"]
    code = await handle.wait()
    assert code == 0 and not handle.timed_out, (
        f"worker exited {code}; timed_out={handle.timed_out}; stderr={handle.stderr_tail}"
    )
    return lines


@windows_only
def test_forwarding_headers_do_not_unlock_state_changes_for_a_worker(live):
    kind, port, token = live
    # The detection is best effort but must work for uvicorn: default config = proxy headers on.
    assert api_security._proxy_headers_detected is (kind == "uvicorn-default")
    lines = asyncio.run(_run_worker(["-c", CLIENT, str(port), token, json.dumps(VARIANTS), json.dumps(BODY)]))
    results = [json.loads(ln) for ln in lines if ln.startswith("{")]
    assert len(results) == len(VARIANTS), lines
    for r in results:
        assert r["status"] == 403, r
        assert r["code"] in ("worker_origin_refused", "peer_spoofed"), r
    assert results[0]["code"] == "worker_origin_refused"
    assert all(r["code"] == "peer_spoofed" for r in results[1:]), results
    # A legitimate non-worker client (this test process) is served...
    r = httpx.put(f"http://127.0.0.1:{port}/api/settings", json=BODY, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    assert r.status_code == 200, r.text
    # ...but not with forwarding headers on a loopback connection (no proxy is ever legitimate here).
    r = httpx.put(f"http://127.0.0.1:{port}/api/settings", json=BODY, timeout=10,
                  headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": "8.8.8.8"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "peer_spoofed"
    assert httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=10).status_code == 200


# --------------------------------------------------------------------------- decision policy (no sockets)


def _scope(method="PUT", client=("127.0.0.1", 50123), server=("127.0.0.1", 8765), headers=(), kind="http") -> dict:
    scope = {"type": kind, "path": "/api/settings", "headers": list(headers), "client": client, "server": server,
             "query_string": b""}
    if kind == "http":
        scope["method"] = method
    return scope


async def _inner(scope, receive, send) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _call(mw, scope) -> list[dict]:
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(msg):
        sent.append(msg)

    asyncio.run(mw(scope, receive, send))
    return sent


def _code(sent) -> str | None:
    if sent[0].get("type") == "websocket.close":
        return sent[0]["reason"]
    return json.loads(sent[1]["body"])["error"]["code"] if sent[0]["status"] == 403 else None


def test_non_ip_or_port_zero_client_fails_closed():
    """What a rewrite produces (`testclient`, port 0) is unidentifiable: reads only, never writes or WS."""
    calls: list = []
    mw = WorkerOriginMiddleware(_inner, classify=lambda *a: calls.append(a) or PeerVerdict("user"))
    for client in (("testclient", 50000), ("127.0.0.1", 0), ("::1", 0), ("8.8.8.8", None), None, ("not-an-ip", 1)):
        assert _code(_call(mw, _scope("PUT", client=client))) == "peer_unresolved", client
        assert _code(_call(mw, _scope("GET", client=client, kind="websocket"))) == "peer_unresolved", client
        assert _code(_call(mw, _scope("GET", client=client))) is None, client
    assert calls == []  # never classified


def test_test_client_is_trusted_only_when_the_app_says_so(tmp_path: Path):
    mw = WorkerOriginMiddleware(_inner, classify=lambda *a: PeerVerdict("user"), trust_test_client=True)
    assert _code(_call(mw, _scope("PUT", client=("testclient", 50000), server=("testserver", 80)))) is None
    # ...but never together with forwarding headers
    hdr = [(b"x-forwarded-for", b"8.8.8.8")]
    assert _code(_call(mw, _scope("PUT", client=("testclient", 50000), server=("testserver", 80), headers=hdr))) == "peer_spoofed"
    # production default: off
    assert api_security.WorkerOriginMiddleware(_inner).trust_test_client is False
    app = create_app(data_dir=tmp_path / "data", providers={"mock": MockProvider()}, start_scheduler=False,
                     trust_test_client=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        h = {"Authorization": f"Bearer {app.state.rt.token}"}
        assert c.get("/api/status", headers=h).status_code == 200
        r = c.put("/api/settings", json=BODY, headers=h)
        assert r.status_code == 403 and r.json()["error"]["code"] == "peer_unresolved"


def test_forwarding_headers_on_loopback_are_refused_for_every_method(caplog):
    mw = WorkerOriginMiddleware(_inner, classify=lambda *a: PeerVerdict("user"))
    for name in (b"x-forwarded-for", b"X-Forwarded-Host", b"forwarded", b"x-real-ip", b"x-forwarded-proto"):
        for method in ("GET", "PUT", "DELETE"):
            with caplog.at_level(logging.WARNING, logger="orchestrator.api.security"):
                assert _code(_call(mw, _scope(method, headers=[(name, b"8.8.8.8")]))) == "peer_spoofed"
        assert _code(_call(mw, _scope("GET", headers=[(name, b"1.2.3.4")], kind="websocket"))) == "peer_spoofed"
    assert any("SECURITY:" in r.getMessage() and "forwarding headers" in r.getMessage() for r in caplog.records)
    # A genuinely remote connection (exposed bind) keeps the old behaviour: headers alone do not refuse...
    remote = _scope("PUT", client=("192.0.2.10", 40000), server=("192.0.2.1", 8765), headers=[(b"x-forwarded-for", b"8.8.8.8")])
    assert _code(_call(mw, remote)) is None
    # ...unless the server was detected to rewrite addresses from proxy headers.
    api_security.note_proxy_headers_enabled(True)
    try:
        assert _code(_call(mw, remote)) == "peer_spoofed"
    finally:
        api_security.note_proxy_headers_enabled(False)


def test_non_local_client_on_a_loopback_connection_is_spoofed():
    assert classify_peer("8.8.8.8", 4444, "127.0.0.1", 8765) == PeerVerdict("spoofed")
    assert classify_peer("8.8.8.8", 4444, "::1", 8765) == PeerVerdict("spoofed")
    assert classify_peer("testclient", 50000, "127.0.0.1", 8765) == PeerVerdict("unresolved")
    assert classify_peer("127.0.0.1", 0, "127.0.0.1", 8765) == PeerVerdict("unresolved")
    mw = WorkerOriginMiddleware(_inner)  # the real classifier
    for method in ("GET", "PUT"):
        assert _code(_call(mw, _scope(method, client=("8.8.8.8", 4444)))) == "peer_spoofed"


def test_state_changing_requests_and_ws_are_never_served_from_the_cache():
    """N7: the verdict cache is keyed by client endpoint; only GET/HEAD may reuse a verdict."""
    calls: list = []

    def classify(*a):
        calls.append(a)
        return PeerVerdict("user")

    mw = WorkerOriginMiddleware(_inner, classify=classify)
    for _ in range(3):
        _call(mw, _scope("PUT"))
        _call(mw, _scope("POST"))
        _call(mw, _scope("DELETE"))
        _call(mw, _scope("GET", kind="websocket"))
    assert len(calls) == 12
    calls.clear()
    for _ in range(3):
        _call(mw, _scope("GET"))
        _call(mw, _scope("HEAD"))
    assert len(calls) == 1  # one resolution, reused by the safe methods within the TTL


def test_cached_user_verdict_does_not_cover_a_later_write_from_a_worker_on_the_same_endpoint():
    verdicts = iter([PeerVerdict("user"), PeerVerdict("worker", 77)])
    mw = WorkerOriginMiddleware(_inner, classify=lambda *a: next(verdicts))
    assert _code(_call(mw, _scope("GET"))) is None
    assert _code(_call(mw, _scope("PUT"))) == "worker_origin_refused"
