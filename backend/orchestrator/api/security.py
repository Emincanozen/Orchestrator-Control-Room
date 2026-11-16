"""Host / Origin validation (ARCHITECTURE §12) as pure ASGI middleware.

Blocks DNS-rebinding (unexpected Host) and cross-site requests (unexpected
Origin) for both HTTP and WebSocket. Requests without an Origin header
(CLI, curl) are allowed; they still need the bearer token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from starlette.types import ASGIApp, Receive, Scope, Send

from orchestrator.security import peer, worker_job

_log = logging.getLogger(__name__)


def _host_only(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):  # [::1]:8765
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return host.rsplit(":", 1)[0] if ":" in host else host


class HostOriginMiddleware:
    def __init__(self, app: ASGIApp, get_rules: Callable[[], tuple[set[str], set[str]]]):
        self.app = app
        self.get_rules = get_rules

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        hosts, origins = self.get_rules()
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = _host_only(headers.get("host", ""))
        origin = headers.get("origin")
        problem = None
        if host not in hosts:
            problem = ("invalid_host", "Host header not allowed")
        elif origin is not None and origin.rstrip("/").lower() not in origins:
            problem = ("invalid_origin", "Origin not allowed")
        if problem is None:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403, "reason": problem[1]})
            return
        body = json.dumps({"error": {"code": problem[0], "message": problem[1], "details": {}}}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})


async def _refuse(scope: Scope, send: Send, code: str, message: str) -> None:
    if scope["type"] == "websocket":  # closed before accept, like the Host/Origin refusals
        await send({"type": "websocket.close", "code": 4403, "reason": code})
        return
    body = json.dumps({"error": {"code": code, "message": message, "details": {}}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------------------- worker-origin guard (finding #1)
#
# The guard identifies the client PROCESS from the connection's TCP endpoints, so it must only ever see the real
# socket peer. ASGI servers can rewrite `scope["client"]` from `X-Forwarded-For` (uvicorn does so by default for
# connections from 127.0.0.1 / ::1: `proxy_headers=True`, `forwarded_allow_ips="127.0.0.1"`), and every local
# process connects from loopback, so with those defaults any worker could pick the address the guard sees
# (re-review N1). Therefore:
#   * `orchestrator serve` runs uvicorn with `proxy_headers=False, forwarded_allow_ips=""`. Anyone serving the
#     app another way MUST do the same, e.g.
#         uvicorn orchestrator.api.app:create_app --factory --no-proxy-headers --forwarded-allow-ips ""
#   * independently of how the app is served, this middleware fails closed:
#       - a request carrying `Forwarded` / `X-Forwarded-*` / `X-Real-IP` on a loopback connection is refused
#         for every method (nothing legitimate proxies to a loopback-only server) and logged as `SECURITY:`;
#       - a client host that is not an IP literal, or a client port of 0/None (what a rewrite produces), is
#         `unresolved` (reads only), except Starlette's TestClient when the app was built with
#         `trust_test_client=True` (test fixtures only);
#       - on a loopback connection every genuine peer is local, so a non-local client address can only be a
#         rewritten one and is refused for every method;
#   * the app lifespan detects (best effort) a uvicorn started with proxy headers and logs a SECURITY error;
#     forwarding headers are then refused on every connection, not only loopback ones.
# uvicorn does not expose the transport/socket in the ASGI scope (checked 0.54: only the "client"/"server"
# tuples), so the peer cannot be re-derived from the socket here.

ALWAYS_ALLOWED_PATHS = frozenset({"/api/health"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CACHEABLE_METHODS = frozenset({"GET", "HEAD"})  # state-changing requests and WS upgrades re-resolve every time
PEER_CACHE_TTL = 1.0  # seconds; keep-alive bursts reuse the verdict, a reused client port is re-resolved soon
PEER_CACHE_MAX = 1024
PEER_RETRY_DELAY = 0.005  # one re-read of the TCP table before a peer counts as unresolvable
TEST_CLIENT_HOST = "testclient"  # Starlette TestClient's scope["client"][0]
FORWARDING_HEADERS = frozenset({b"forwarded", b"x-real-ip", b"true-client-ip", b"x-client-ip"})
FORWARDING_PREFIX = b"x-forwarded-"

# Default for `create_app(trust_test_client=None)`. False in production; tests/conftest.py sets it to True for
# the test process only. It is a Python attribute, not an environment variable or a setting: neither an API
# call nor a worker's environment can change it.
TRUST_TEST_CLIENT_DEFAULT = False

_proxy_headers_detected = False


@dataclass(frozen=True)
class PeerVerdict:
    kind: str  # "user" | "worker" | "unresolved" | "spoofed" | "not_applicable"
    pid: Optional[int] = None


def classify_peer(client_host: str, client_port: int, server_host: Optional[str], server_port: Optional[int]) -> PeerVerdict:
    """Blocking: resolve the client's PID and decide whether it is an agent worker. Run in a thread.

    Fails closed: an unusable client endpoint is `unresolved`; on a loopback server endpoint a non-local client
    address is `spoofed` (it was rewritten, e.g. from X-Forwarded-For)."""
    addr = peer.parse_ip(client_host)
    if addr is None or not isinstance(client_port, int) or client_port <= 0:
        return PeerVerdict("unresolved")
    server_addr = peer.parse_ip(server_host) if server_host else None
    local = peer.is_local_address(addr)
    if server_addr is not None and server_addr.is_loopback and not local:
        return PeerVerdict("spoofed")
    if not worker_job.active():
        return PeerVerdict("user")  # no worker was ever started by this process (and none survive it)
    pid = peer.owner_pid(client_host, client_port, server_host, server_port)
    if pid is None:
        time.sleep(PEER_RETRY_DELAY)
        pid = peer.owner_pid(client_host, client_port, server_host, server_port)
    if pid is None:
        return PeerVerdict("unresolved" if local else "user")  # genuinely remote host (exposed bind): not our worker
    member = worker_job.is_worker(pid)
    if member is True:
        return PeerVerdict("worker", pid)
    if member is None:
        return PeerVerdict("unresolved", pid)
    return PeerVerdict("user", pid)


def forwarding_headers(scope: Scope) -> list[str]:
    """Names of proxy/forwarding headers present on the request."""
    found = []
    for name, _value in scope.get("headers", []):
        n = name.lower()
        if n in FORWARDING_HEADERS or n.startswith(FORWARDING_PREFIX):
            found.append(n.decode("latin-1"))
    return found


def note_proxy_headers_enabled(enabled: bool) -> None:
    """Record that the ASGI server rewrites scope["client"] from proxy headers (see `detect_proxy_headers`)."""
    global _proxy_headers_detected
    _proxy_headers_detected = bool(enabled)


def detect_proxy_headers() -> Optional[bool]:
    """Best effort, from inside the app lifespan: does the uvicorn that runs us rewrite the client address from
    proxy headers? Walks the calling frames for uvicorn's lifespan object (it holds the Config). None when not
    detectable (another ASGI server, or uvicorn internals changed)."""
    frame = sys._getframe(1)
    depth = 0
    while frame is not None and depth < 100:
        obj = frame.f_locals.get("self")
        config = getattr(obj, "config", None)
        if config is not None and type(config).__module__.startswith("uvicorn") and hasattr(config, "proxy_headers"):
            allowed = getattr(config, "forwarded_allow_ips", None)
            if isinstance(allowed, (list, tuple, set)):
                allowed = ",".join(str(a) for a in allowed)
            if allowed is None:  # uvicorn default: $FORWARDED_ALLOW_IPS or 127.0.0.1
                allowed = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
            return bool(config.proxy_headers) and bool(str(allowed).strip())
        frame = frame.f_back
        depth += 1
    return None


RefusalHook = Callable[[int, str, str], None]


class WorkerOriginMiddleware:
    """Refuse HTTP and WebSocket requests whose client process is an agent worker (or a descendant of one).

    Runs before authentication: a worker can read the API token, so the token alone does not prove the caller
    is the user. A peer whose PID cannot be resolved may still read (GET/HEAD/OPTIONS) but may not change
    state or open the event stream. A peer whose address was evidently rewritten (forwarding headers or a
    non-local address on a loopback connection) is refused for every method. `/api/health` is always allowed."""

    def __init__(self, app: ASGIApp, on_refused: Optional[RefusalHook] = None,
                 classify: Callable[..., PeerVerdict] = classify_peer, *, trust_test_client: bool = False) -> None:
        self.app = app
        self.on_refused = on_refused
        self.classify = classify
        self.trust_test_client = bool(trust_test_client)
        self._cache: dict[tuple[str, int, Optional[int]], tuple[float, PeerVerdict]] = {}

    async def _verdict(self, scope: Scope, method: str, path: str) -> PeerVerdict:
        client = scope.get("client")
        server = scope.get("server") or (None, None)
        host = str(client[0]) if client else ""
        port = client[1] if client and len(client) > 1 else None
        fwd = forwarding_headers(scope)
        if self.trust_test_client and host == TEST_CLIENT_HOST and not fwd:
            return PeerVerdict("not_applicable")
        server_addr = peer.parse_ip(str(server[0])) if server[0] else None
        loopback_conn = server_addr is None or server_addr.is_loopback
        if fwd and (loopback_conn or _proxy_headers_detected):
            _log.warning("SECURITY: refused %s %s carrying forwarding headers (%s) on a loopback connection; "
                         "client reported as %r", method, path, ",".join(sorted(set(fwd))), host)
            return PeerVerdict("spoofed")
        if peer.parse_ip(host) is None or not isinstance(port, int) or port <= 0:
            return PeerVerdict("unresolved")
        cacheable = method in CACHEABLE_METHODS
        key = (host, port, server[1])
        now = time.monotonic()
        if cacheable:
            hit = self._cache.get(key)
            if hit is not None and hit[0] > now:
                return hit[1]
        verdict = await asyncio.to_thread(self.classify, host, port, server[0], server[1])
        if verdict.kind == "spoofed":
            _log.warning("SECURITY: refused %s %s: client address %s is not local on a loopback connection "
                         "(rewritten peer address?)", method, path, host)
        if cacheable:
            if len(self._cache) >= PEER_CACHE_MAX:
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
                if len(self._cache) >= PEER_CACHE_MAX:
                    self._cache.clear()
            self._cache[key] = (now + PEER_CACHE_TTL, verdict)
        return verdict

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or scope.get("path") in ALWAYS_ALLOWED_PATHS:
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "WEBSOCKET") if scope["type"] == "http" else "WEBSOCKET"
        path = str(scope.get("path", ""))  # never the query string (it may carry ?token=)
        verdict = await self._verdict(scope, method, path)
        if verdict.kind == "worker":
            _log.warning("SECURITY: refused %s %s from agent worker process pid=%s", method, path, verdict.pid)
            if self.on_refused is not None:
                try:
                    self.on_refused(verdict.pid or 0, method, path)
                except Exception:  # noqa: BLE001 - reporting must never break the refusal
                    _log.exception("worker-origin refusal hook failed")
            await _refuse(scope, send, "worker_origin_refused",
                          "requests from agent worker processes are refused; the API is for the user only")
            return
        if verdict.kind == "spoofed":
            await _refuse(scope, send, "peer_spoofed",
                          "the client address was rewritten (proxy/forwarding headers are not accepted); refused")
            return
        if verdict.kind == "unresolved" and method not in SAFE_METHODS:
            _log.warning("SECURITY: refused %s %s: the client process could not be identified (pid=%s)", method, path, verdict.pid)
            await _refuse(scope, send, "peer_unresolved",
                          "the client process could not be identified; state-changing requests are refused")
            return
        await self.app(scope, receive, send)


def normalize_origins(values: Any) -> set[str]:
    return {str(v).rstrip("/").lower() for v in (values or [])}


BIND_PORT_ENV = "ORCHESTRATOR_BIND_PORT"
DEV_ENV = "ORCHESTRATOR_DEV"
DEV_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")  # the Vite dev server; never in production


def dev_mode() -> bool:
    """Explicit developer mode (ORCHESTRATOR_DEV=1): enables /docs, /redoc, /openapi.json and the 5173 dev origins."""
    return os.environ.get(DEV_ENV, "").strip() == "1"


def effective_origins(configured: Any) -> set[str]:
    """Configured origins plus the loopback origins of the port the server actually listens on
    (`orchestrator serve --port N` exports ORCHESTRATOR_BIND_PORT), so another port needs no settings change."""
    origins = normalize_origins(configured)
    port = os.environ.get(BIND_PORT_ENV, "").strip()
    if port.isdigit():
        origins |= {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    if dev_mode():
        origins |= set(DEV_ORIGINS)
    return origins
