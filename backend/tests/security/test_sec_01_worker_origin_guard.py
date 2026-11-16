"""Finding #1 (HIGH), worker-origin guard: the API refuses requests that come from processes it started for
agents (and their descendants), even when they present the right token.

Real Win32 calls throughout: a real uvicorn server on an ephemeral port, real child / grandchild Python
processes spawned through the same code paths the adapters and the verifier use.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import textwrap
import threading
import time
from pathlib import Path

import httpx
import psutil
import pytest
from fastapi.testclient import TestClient

from orchestrator.api import security as api_security
from orchestrator.api.app import create_app
from orchestrator.api.security import PeerVerdict, WorkerOriginMiddleware, classify_peer
from orchestrator.providers.mock import MockProvider
from orchestrator.providers.process import run_capture, spawn_process
from orchestrator.scheduler.verification import run_command
from orchestrator.security import peer, sandbox, worker_job

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows-only")

# Child: one HTTP request to the server, prints {"status": ..., "code": ...}.
CLIENT = textwrap.dedent(
    """
    import json, sys, urllib.request, urllib.error
    port, token, method = sys.argv[1], sys.argv[2], sys.argv[3]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status", method=method,
                                 headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(json.dumps({"status": r.status, "code": None}), flush=True)
    except urllib.error.HTTPError as e:
        body = json.loads(e.read() or b"{}")
        print(json.dumps({"status": e.code, "code": body.get("error", {}).get("code")}), flush=True)
    """
)

# Child that starts a grandchild running CLIENT and relays its output.
PARENT = textwrap.dedent(
    """
    import subprocess, sys
    out = subprocess.run([sys.executable, "-c", sys.argv[1], *sys.argv[2:]], capture_output=True, text=True, timeout=30)
    print(out.stdout.strip(), flush=True)
    """
)

# Child that tries to start a grandchild outside the job.
BREAKAWAY = textwrap.dedent(
    """
    import subprocess, sys
    try:
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"], creationflags=0x01000000)
        print("STARTED", p.pid, flush=True)
        p.wait()
    except OSError as e:
        print("DENIED", getattr(e, "winerror", None), flush=True)
    """
)

# Child: raw WebSocket upgrade, prints the status line it got back.
WS_CLIENT = textwrap.dedent(
    """
    import socket, sys
    port, token = int(sys.argv[1]), sys.argv[2]
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall((f"GET /ws HTTP/1.1\\r\\nHost: 127.0.0.1:{port}\\r\\nUpgrade: websocket\\r\\nConnection: Upgrade\\r\\n"
               "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\\r\\nSec-WebSocket-Version: 13\\r\\n"
               f"Sec-WebSocket-Protocol: orch, orch.token.{token}\\r\\n\\r\\n").encode())
    data = b""
    while b"\\r\\n" not in data:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    print(data.split(b"\\r\\n", 1)[0].decode(), flush=True)
    """
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(tmp_path: Path):
    """The real app served by uvicorn in a background thread. Yields (port, token)."""
    import uvicorn

    app = create_app(data_dir=tmp_path / "data", providers={"mock": MockProvider()}, start_scheduler=False)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        assert thread.is_alive(), "server thread died"
        time.sleep(0.05)
    try:
        yield port, app.state.rt.token
    finally:
        server.should_exit = True
        thread.join(15)


async def _run_worker(args: list[str]) -> list[str]:
    """Spawn through the provider spawn path (what CliAdapterBase uses) and collect stdout lines."""
    handle = await spawn_process([sys.executable, *args], cwd=os.getcwd(), env=dict(os.environ), timeout=60)
    lines = [line async for kind, line in handle.stream() if kind == "stdout"]
    await handle.wait()
    return lines


def _last_json(lines: list[str]) -> dict:
    return json.loads([ln for ln in lines if ln.startswith("{")][-1])


# --------------------------------------------------------------------------- live server, real processes


@windows_only
def test_worker_child_refused_but_user_accepted(live):
    port, token = live
    lines = asyncio.run(_run_worker(["-c", CLIENT, str(port), token, "GET"]))
    assert _last_json(lines) == {"status": 403, "code": "worker_origin_refused"}
    lines = asyncio.run(_run_worker(["-c", CLIENT, str(port), token, "POST"]))
    assert _last_json(lines)["code"] == "worker_origin_refused"
    # The same request from the test process itself (not in the job) goes through, with the token.
    r = httpx.get(f"http://127.0.0.1:{port}/api/status", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    assert r.status_code == 200
    # Health stays reachable for everyone (no token, no guard).
    lines = asyncio.run(_run_worker(["-c", CLIENT.replace("/api/status", "/api/health"), str(port), "x", "GET"]))
    assert _last_json(lines) == {"status": 200, "code": None}


@windows_only
def test_worker_grandchild_refused(live):
    port, token = live
    lines = asyncio.run(_run_worker(["-c", PARENT, CLIENT, str(port), token, "GET"]))
    assert _last_json(lines) == {"status": 403, "code": "worker_origin_refused"}


@windows_only
def test_breakaway_does_not_leave_the_job():
    lines: list[str] = []

    async def go() -> None:
        handle = await spawn_process([sys.executable, "-c", BREAKAWAY], cwd=os.getcwd(), env=dict(os.environ), timeout=30)
        async for kind, line in handle.stream():
            if kind != "stdout":
                continue
            lines.append(line)
            if line.startswith("STARTED"):
                # CreateProcess(CREATE_BREAKAWAY_FROM_JOB) may succeed when an OUTER job allows breakaway, but the
                # process must still be in our job (it has no BREAKAWAY_OK limit).
                assert worker_job.is_in_job(int(line.split()[1])) is True
                await handle.terminate_tree()
        await handle.wait()

    asyncio.run(go())
    assert lines and (lines[0].startswith("DENIED") or lines[0].startswith("STARTED")), lines


@windows_only
def test_websocket_from_worker_closed_before_accept(live):
    port, token = live
    lines = asyncio.run(_run_worker(["-c", WS_CLIENT, str(port), token]))
    status_line = lines[-1]
    assert " 101 " not in status_line and "403" in status_line, status_line


# --------------------------------------------------------------------------- spawn paths really assign


async def _find_child(marker: str, timeout: float = 10.0) -> psutil.Process:
    me = psutil.Process()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for child in me.children(recursive=True):
            try:
                if any(marker in part for part in child.cmdline()):
                    return child
            except psutil.Error:
                continue
        await asyncio.sleep(0.05)
    raise AssertionError(f"child with marker {marker} not found")


@windows_only
def test_provider_spawn_path_assigns_to_job():
    async def go() -> None:
        handle = await spawn_process([sys.executable, "-c", "import time; time.sleep(5)"], cwd=os.getcwd(),
                                     env=dict(os.environ), timeout=30)
        try:
            assert worker_job.is_in_job(handle.pid) is True
            assert worker_job.is_worker(handle.pid) is True
        finally:
            await handle.terminate_tree()
            await handle.wait()

    asyncio.run(go())
    assert worker_job.is_in_job(os.getpid()) is False  # the server itself is never in its own worker job


@windows_only
def test_helper_run_capture_assigns_to_job():
    marker = "orch-helper-marker-7f3a"

    async def go() -> None:
        task = asyncio.create_task(run_capture([sys.executable, "-c", f"import time; time.sleep(3)  # {marker}"], timeout=20))
        child = await _find_child(marker)
        assert worker_job.is_in_job(child.pid) is True
        code, _out, _err = await task
        assert code == 0

    asyncio.run(go())


@windows_only
def test_verifier_spawn_path_assigns_to_job(native_task, monkeypatch):
    marker = "orch-verify-marker-91c2"
    observed = []
    original_resume = sandbox._resume

    def checked_resume(pid):
        # A suspended process can be visible before assignment now that startup is
        # off the event loop. Assert the actual boundary: confinement BEFORE resume.
        assert worker_job.is_in_job(pid) is True
        assert worker_job.is_worker(pid) is True
        observed.append(pid)
        return original_resume(pid)

    monkeypatch.setattr(sandbox, "_resume", checked_resume)

    async def go() -> None:
        result = await run_command([sys.executable, "-c", f"import time; time.sleep(3)  # {marker}"],
                                   cwd=str(native_task), timeout=30, env=dict(os.environ), writable_cwd=True)
        assert result.passed, result
        assert len(observed) == 1

    asyncio.run(go())


# --------------------------------------------------------------------------- decision policy


def _scope(method: str, path: str = "/api/tasks", client=("127.0.0.1", 9), kind: str = "http") -> dict:
    scope = {"type": kind, "path": path, "headers": [], "client": client, "server": ("127.0.0.1", 8765), "query_string": b""}
    if kind == "http":
        scope["method"] = method
    return scope


async def _call(mw: WorkerOriginMiddleware, scope: dict) -> list[dict]:
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b""}

    async def send(msg: dict) -> None:
        sent.append(msg)

    await mw(scope, receive, send)
    return sent


async def _inner(scope, receive, send) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
        return
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_unresolvable_loopback_peer_policy():
    """A loopback endpoint with no row in the real TCP table: reads pass, writes and WS upgrades are refused."""
    mw = WorkerOriginMiddleware(_inner, classify=lambda *a: PeerVerdict("unresolved"))
    for method in ("GET", "HEAD", "OPTIONS"):
        assert asyncio.run(_call(mw, _scope(method)))[0]["status"] == 200
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = asyncio.run(_call(mw, _scope(method)))
        assert sent[0]["status"] == 403
        assert json.loads(sent[1]["body"])["error"]["code"] == "peer_unresolved"
    ws = asyncio.run(_call(mw, _scope("GET", "/ws", kind="websocket")))
    assert ws == [{"type": "websocket.close", "code": 4403, "reason": "peer_unresolved"}]
    assert asyncio.run(_call(mw, _scope("POST", "/api/health")))[0]["status"] == 200  # always allowed


@windows_only
def test_real_classifier_reports_unresolved_for_unknown_loopback_endpoint():
    async def ensure_active() -> None:  # make sure a job exists in this process (classification fast path)
        handle = await spawn_process([sys.executable, "-c", "pass"], cwd=os.getcwd(), env=dict(os.environ), timeout=30)
        await handle.wait()

    asyncio.run(ensure_active())
    port = _free_port()  # nothing listens or connects there
    assert peer.owner_pid("127.0.0.1", port, "127.0.0.1", 8765) is None
    assert classify_peer("127.0.0.1", port, "127.0.0.1", 8765) == PeerVerdict("unresolved")
    assert classify_peer("testclient", 50000, None, None) == PeerVerdict("unresolved")  # fail closed (re-review N1)


@windows_only
def test_owner_pid_resolves_own_connection():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        sport = srv.getsockname()[1]
        with socket.create_connection(("127.0.0.1", sport)) as c:
            conn, _ = srv.accept()
            with conn:
                assert peer.owner_pid("127.0.0.1", c.getsockname()[1], "127.0.0.1", sport) == os.getpid()


def test_worker_verdict_refuses_everything_but_health():
    mw = WorkerOriginMiddleware(_inner, classify=lambda *a: PeerVerdict("worker", 4242))
    seen: list[tuple] = []
    mw.on_refused = lambda pid, method, path: seen.append((pid, method, path))
    sent = asyncio.run(_call(mw, _scope("GET", "/api/status")))
    assert sent[0]["status"] == 403 and json.loads(sent[1]["body"])["error"]["code"] == "worker_origin_refused"
    ws = asyncio.run(_call(mw, _scope("GET", "/ws", kind="websocket")))
    assert ws[0]["type"] == "websocket.close" and ws[0]["code"] == 4403
    assert asyncio.run(_call(mw, _scope("GET", "/api/health")))[0]["status"] == 200
    assert seen == [(4242, "GET", "/api/status"), (4242, "WEBSOCKET", "/ws")]


def test_testclient_unaffected(tmp_path: Path):
    """TestClient's client host is 'testclient' (not an IP): the guard lets it through only because the test
    process trusts the test client (tests/conftest.py); the classifier itself fails closed (re-review N1)."""
    assert api_security.classify_peer("testclient", 50000, "testserver", 80) == PeerVerdict("unresolved")
    assert api_security.TRUST_TEST_CLIENT_DEFAULT is True  # set by tests/conftest.py, never in production
    app = create_app(data_dir=tmp_path / "data", providers={"mock": MockProvider()}, start_scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        token = app.state.rt.token
        r = c.get("/api/status", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        r = c.post("/api/projects", json={}, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 422  # reached validation: not refused by the guard
