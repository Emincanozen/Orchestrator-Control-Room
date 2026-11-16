"""FastAPI application factory.

    app = create_app()                      # production (uvicorn factory)
    app = create_app(providers={...}, data_dir=tmp, extra_routers=[my_router])

Adding a route group in a later milestone: write `api/routes/<area>.py` with
`router = APIRouter(prefix="/api/<area>", dependencies=AUTH)` and append it to
CORE_ROUTERS below (or pass it via `extra_routers`). Services that the new
routes need can be attached in a startup hook (`on_startup=[fn(rt)]`) and
stored in `rt.extras`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from orchestrator import __version__
from orchestrator.api.routes import agents as agents_routes
from orchestrator.api.routes import approvals as approvals_routes
from orchestrator.api.routes import analytics as analytics_routes
from orchestrator.api.routes import knowledge as knowledge_routes
from orchestrator.api.routes import leadership as leadership_routes
from orchestrator.api.routes import objectives as objectives_routes
from orchestrator.api.routes import projects as projects_routes
from orchestrator.api.routes import providers as providers_routes
from orchestrator.api.routes import routing as routing_routes
from orchestrator.api.routes import skills as skills_routes
from orchestrator.api.routes import system as system_routes
from orchestrator.api.routes import tasks as tasks_routes
from orchestrator.api import ws as ws_routes
from orchestrator.api.runtime import Runtime
from orchestrator.api import security as api_security
from orchestrator.api.security import HostOriginMiddleware, WorkerOriginMiddleware, dev_mode, effective_origins
from orchestrator.domain import events as ev
import orchestrator.analytics as analytics
import orchestrator.knowledge as knowledge
import orchestrator.skills as skills
from orchestrator.checkpoints.manager import CheckpointManager
from orchestrator.leader import build_stack
from orchestrator.db.retention import DEFAULT_MAX_AGE_DAYS, DEFAULT_MAX_ROWS, limit_value, prune_events
from orchestrator.config import FRONTEND_DIST, get_paths, load_yaml, mock_enabled, real_providers_disabled
from orchestrator.domain.states import InvalidTransition
from orchestrator.errors import OrchestratorError
from orchestrator.providers.base import ProviderAdapter
from orchestrator.providers.registry import ProviderRegistry
from orchestrator.scheduler.executor import TaskExecutor
from orchestrator.scheduler.ports import LeaderPort, RouterPort
from orchestrator.scheduler.recovery import recover
from orchestrator.scheduler.scheduler import Scheduler
from orchestrator.security.approvals import ApprovalService
from orchestrator.security.auth import load_or_create_token
from orchestrator.security.instance_lock import InstanceLock
from orchestrator.security import sandbox, worker_job
from orchestrator.security.paths import PathPolicyError, resolve_inside
from orchestrator.services.agents import AgentService
from orchestrator.services.context import AppContext
from orchestrator.services.projects import ProjectService
from orchestrator.services.providers import ProviderService, merge_models_cfg, mock_models_cfg
from orchestrator.services.routing import RoutingService
from orchestrator.services.usage import UsageMonitor
from orchestrator.services.tasks import TaskService

log = logging.getLogger(__name__)

CORE_ROUTERS: list[APIRouter] = [
    system_routes.public,
    system_routes.router,
    projects_routes.router,
    tasks_routes.router,
    agents_routes.router,
    ws_routes.router,
    approvals_routes.router,
    providers_routes.router,
    routing_routes.router,
    leadership_routes.router,
    objectives_routes.router,
]

DEFAULT_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
RETENTION_INTERVAL_SECONDS = 3600.0
WORKER_NOTICE_INTERVAL_SECONDS = 60.0

StartupHook = Callable[[Runtime], Awaitable[None]]


def default_registry(data_dir: Any = None) -> ProviderRegistry:
    """The provider registry for production: real claude/codex/antigravity adapters
    (unless ORCHESTRATOR_DISABLE_REAL_PROVIDERS=1) plus the mock only when ORCHESTRATOR_ENABLE_MOCK=1."""
    registry = ProviderRegistry({}) if real_providers_disabled() else ProviderRegistry.build_default(data_dir=data_dir)
    if mock_enabled():
        from orchestrator.providers.mock import MockProvider

        registry.register(MockProvider())
    return registry


def default_providers() -> dict[str, ProviderAdapter]:
    registry = default_registry()
    return {name: registry.get(name) for name in registry.names()}


def install_log_filter() -> None:
    from orchestrator.security.logfilter import install_on_uvicorn_loggers

    install_on_uvicorn_loggers()


async def _default_leader(stack: Any) -> None:
    try:
        await stack.leadership.ensure_default_leader()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - never fail startup over the default leader
        log.exception("recording the default leader failed")


async def _retention_loop(ctx: AppContext, interval: float = RETENTION_INTERVAL_SECONDS) -> None:
    """Prune the events table at startup and then periodically. Never fails the server."""
    while True:
        try:
            policies = await ctx.policies()
            await prune_events(
                ctx.db,
                max_age_days=limit_value(policies, "events_max_age_days", DEFAULT_MAX_AGE_DAYS),
                max_rows=limit_value(policies, "events_max_rows", DEFAULT_MAX_ROWS),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("events retention failed")
        await asyncio.sleep(interval)


def _error(status: int, code: str, message: str, details: Optional[dict[str, Any]] = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "details": details or {}}})


_HTTP_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed", 409: "conflict", 422: "validation_failed"}


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(OrchestratorError)
    async def _orch(_req: Request, exc: OrchestratorError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(InvalidTransition)
    async def _transition(_req: Request, exc: InvalidTransition) -> JSONResponse:
        return _error(409, "invalid_transition", str(exc), {"from": exc.current.value, "to": exc.target.value})

    @app.exception_handler(PathPolicyError)
    async def _path(_req: Request, exc: PathPolicyError) -> JSONResponse:
        return _error(403, "path_policy", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return _error(422, "validation_failed", "request validation failed", {"errors": errors})

    @app.exception_handler(StarletteHTTPException)
    async def _http(_req: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, _HTTP_CODES.get(exc.status_code, "error"), str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(_req: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return _error(500, "internal_error", "internal server error")


def _install_static(app: FastAPI, dist: Path) -> None:
    index = dist / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Any:
        if full_path.startswith(("api/", "ws")):
            return _error(404, "not_found", "not found")
        if full_path:
            try:
                target = resolve_inside(full_path, [dist], base=str(dist))
                if target.is_file():
                    return FileResponse(target)
            except PathPolicyError:
                return _error(404, "not_found", "not found")
        return FileResponse(index)


def create_app(
    *,
    data_dir: Optional[str | os.PathLike[str]] = None,
    providers: Optional[Mapping[str, ProviderAdapter]] = None,
    router: Optional[RouterPort] = None,
    leader: Optional[LeaderPort] = None,
    extra_routers: Iterable[APIRouter] = (),
    on_startup: Iterable[StartupHook] = (),
    start_scheduler: bool = True,
    allowed_hosts: Optional[Iterable[str]] = None,
    celebrate_seconds: float = 3.0,
    poll_interval: float = 1.0,
    frontend_dist: Optional[Path] = None,
    use_routing: Optional[bool] = None,
    extra_models_cfg: Optional[Mapping[str, Any]] = None,
    use_leader: Optional[bool] = None,
    trust_test_client: Optional[bool] = None,
) -> FastAPI:
    """`use_routing`: wire the real RoutingService as the executor's router. Default: yes when the app
    builds its own providers (production), no when `providers=` is injected (tests keep DefaultRouter
    unless they ask for routing). An explicit `router=` always wins. `extra_models_cfg` overlays extra
    provider sections (e.g. fake providers) on config/models.yaml for routing and /api/models.
    `use_leader` (M3): wire the real LeaderController as the executor's LeaderPort (and record a default leader
    on first start). Same default as `use_routing`; an explicit `leader=` always wins. The leadership services
    (settings, switch, advisor, objectives, decisions) exist either way.
    `trust_test_client`: let Starlette's TestClient (client host "testclient", not an IP) through the
    worker-origin guard. Test fixtures only; None = `api_security.TRUST_TEST_CLIENT_DEFAULT` (False in
    production, set by tests/conftest.py). Every other non-IP client is treated as unidentifiable."""
    hosts = set(DEFAULT_ALLOWED_HOSTS) | {h.lower() for h in (allowed_hosts or [])}
    extra = list(extra_routers)
    # The skills manager is part of the default app; a caller that mounts the skills router itself
    # (tests with an injected source policy) supplies its own startup and keeps full control.
    default_skills = skills_routes.router not in extra
    # Knowledge and analytics follow the same rule: default unless the caller mounts them itself.
    default_knowledge = knowledge_routes.router not in extra
    default_analytics = analytics_routes.router not in extra
    hooks = [
        *([skills.startup] if default_skills else []),
        *([knowledge.startup] if default_knowledge else []),
        *([analytics.startup] if default_analytics else []),
        *on_startup,
    ]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        paths = get_paths(data_dir)
        # Never elevated (N2 re-review S7): every child would inherit administrator rights.
        sandbox.refuse_elevated()
        # Fail closed (re-review N5): no job object = no way to tell agent processes from the user.
        worker_job.ensure_job()
        proxied = api_security.detect_proxy_headers()
        api_security.note_proxy_headers_enabled(bool(proxied))
        if proxied:
            log.error("SECURITY: the ASGI server rewrites the client address from proxy headers (uvicorn "
                      "proxy_headers=True). Start it with --no-proxy-headers --forwarded-allow-ips \"\" (as "
                      "`orchestrator serve` does). Requests carrying forwarding headers are refused.")
        # Exclusive lock on the data dir BEFORE anything else (recovery would kill the other instance's live runs).
        lock = InstanceLock(paths.data_dir).acquire()
        paths.clean_tmp()  # recreated (owner-only, inside the data dir) by AppContext.open -> paths.ensure()
        try:
            async with _serve(app, paths):
                yield
        finally:
            lock.release()

    @asynccontextmanager
    async def _serve(app: FastAPI, paths: Any):
        ctx = AppContext.open(paths)
        registry = ProviderRegistry(dict(providers)) if providers is not None else default_registry(paths.data_dir)
        models_cfg = load_yaml("models.yaml", ctx.config_dir)
        if "mock" in registry.names():  # explicit registration so the mock is routable; real rules untouched
            models_cfg = merge_models_cfg(models_cfg, mock_models_cfg())
        models_cfg = merge_models_cfg(models_cfg, extra_models_cfg)
        psvc = ProviderService(ctx, registry, models_cfg)
        provs = psvc.adapters
        usage = UsageMonitor(ctx)
        psvc.usage = usage
        routing = RoutingService(ctx, psvc, usage)
        wire_routing = use_routing if use_routing is not None else providers is None
        active_router = router if router is not None else (routing if wire_routing else None)
        if wire_routing:
            usage.alternative_finder = routing.find_candidate
        agents = AgentService(ctx, celebrate_seconds=celebrate_seconds)
        stack = build_stack(ctx, psvc, routing, provs, agents, usage)
        wire_leader = use_leader if use_leader is not None else providers is None
        active_leader = leader if leader is not None else (stack.controller if wire_leader else None)
        executor = TaskExecutor(ctx, provs, router=active_router, leader=active_leader, agents=agents, usage=usage)
        scheduler = Scheduler(ctx, executor, poll_interval=poll_interval, quota_check=usage.quota_check)
        stack.attach_scheduler(scheduler)
        policies = await ctx.policies()
        rt = Runtime(
            ctx=ctx, providers=provs, agents=agents, executor=executor, scheduler=scheduler, tasks=TaskService(ctx),
            projects=ProjectService(ctx), approvals=ApprovalService(ctx), checkpoints=CheckpointManager(ctx),
            token=load_or_create_token(paths.token_path),
            allowed_origins=effective_origins(policies.security.get("allowed_origins")),
            allowed_hosts=hosts,
        )
        rt.extras.update(providers=psvc, usage=usage, routing=routing, leader=stack)
        app.state.rt = rt
        await agents.seed()
        rt.recovery_report = await recover(ctx, agents)
        for hook in hooks:
            await hook(rt)
        install_log_filter()  # whatever started uvicorn: never log `?token=` query values
        psvc.start_background_refresh()  # detection never blocks or fails startup
        default_leader_task: Optional[asyncio.Task] = None
        if wire_leader:  # first start: once detection finished, record the deterministic default leader
            if await stack.leadership.leader() is None:
                stack.leadership.default_state = "detecting"  # truthful from the very first /api/status
            default_leader_task = asyncio.create_task(_default_leader(stack), name="default-leader")
        retention_task = asyncio.create_task(_retention_loop(ctx), name="events-retention")
        if (start_scheduler and os.name == "nt" and sandbox.supported()
                and os.environ.get("ORCHESTRATOR_NO_RUNTIME_PREWARM", "").strip() != "1"):
            # Pay the cold runtime pin/hash/ACL pass once, off the request path, instead of inside the first
            # verification command's deadline. Daemon: never delays shutdown, which serializes on the lease lock.
            threading.Thread(target=sandbox.prewarm_verification_runtimes, name="runtime-prewarm",
                             daemon=True).start()
        if start_scheduler:
            await scheduler.start()
        try:
            yield
        finally:
            retention_task.cancel()
            await asyncio.gather(retention_task, return_exceptions=True)
            if default_leader_task is not None:
                default_leader_task.cancel()
                await asyncio.gather(default_leader_task, return_exceptions=True)
            await stack.shutdown()
            await scheduler.stop()
            await psvc.shutdown()
            await agents.shutdown()
            ctx.close()
            if os.name == "nt":
                from orchestrator.security import python_runtime

                try:
                    if sandbox.retained_sessions():  # retry deferred finishes whose job exit is now verifiable
                        await asyncio.to_thread(sandbox.settle_retained_sessions)
                    python_runtime.shutdown_shared_runtimes()  # unreferenced pins only; uncertain ones stay retained
                except Exception:  # noqa: BLE001 - process exit releases the handles anyway
                    log.exception("shared runtime release at shutdown failed")

    # Interactive docs and the schema are developer tools: off unless ORCHESTRATOR_DEV=1 (they were unauthenticated).
    docs = {} if dev_mode() else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(title="AI Orchestrator", version=__version__, lifespan=lifespan, **docs)

    def rules() -> tuple[set[str], set[str]]:
        rt: Optional[Runtime] = getattr(app.state, "rt", None)
        if rt is None:
            return hosts, set()
        return rt.allowed_hosts, rt.allowed_origins

    notice_at: dict[int, float] = {}
    notice_tasks: set[asyncio.Task] = set()

    def worker_refused(pid: int, method: str, path: str) -> None:
        """Record a worker-origin refusal as a system notice (at most one per worker PID per minute)."""
        rt: Optional[Runtime] = getattr(app.state, "rt", None)
        now = time.monotonic()
        if rt is None or now - notice_at.get(pid, -1e9) < WORKER_NOTICE_INTERVAL_SECONDS:
            return
        if len(notice_at) > 256:
            notice_at.clear()
        notice_at[pid] = now
        task = asyncio.get_running_loop().create_task(rt.ctx.bus.emit(ev.SYSTEM_NOTICE, payload={
            "level": "warning", "security": "worker_origin_refused", "pid": pid, "method": method, "path": path,
            "message": f"SECURITY: refused an API request from agent worker process {pid} ({method} {path})",
        }))
        notice_tasks.add(task)
        task.add_done_callback(notice_tasks.discard)

    # Order: Starlette runs the LAST added middleware first. Host/Origin (cheap) runs first, then the
    # worker-origin guard (finding #1), then routing and per-route authentication.
    trust_tc = api_security.TRUST_TEST_CLIENT_DEFAULT if trust_test_client is None else bool(trust_test_client)
    app.add_middleware(WorkerOriginMiddleware, on_refused=worker_refused, trust_test_client=trust_tc)
    app.add_middleware(HostOriginMiddleware, get_rules=rules)
    _install_error_handlers(app)
    defaults = [
        *([skills_routes.router] if default_skills else []),
        *([knowledge_routes.router] if default_knowledge else []),
        *([analytics_routes.router] if default_analytics else []),
    ]
    for r in [*CORE_ROUTERS, *defaults, *extra]:
        app.include_router(r)
    app.include_router(ws_routes.ws_router)
    dist = frontend_dist if frontend_dist is not None else FRONTEND_DIST
    if (dist / "index.html").is_file():
        _install_static(app, dist)
    return app
