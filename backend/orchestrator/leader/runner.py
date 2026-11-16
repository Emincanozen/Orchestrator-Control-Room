"""CallRunner: recorded, bounded leader/advisor provider calls that need not belong to a task.

Objective decomposition, leader-switch validation and advisor consults must run even while a worker run
of the same task is alive, so they cannot use the executor's one-live-run-per-task path. This runner
writes the same records (a `runs` row with idempotency key, `router.decision` + `run.*` events, usage
sink, seat activity) with `task_id` possibly NULL. Every call is read-only and has a hard timeout.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from orchestrator.db import runs as runs_repo
from orchestrator.db import records
from orchestrator.db.database import Tx
from orchestrator.domain import events as ev
from orchestrator.domain.models import Task
from orchestrator.domain.states import AgentActivity, RunPhase, RunStatus, TaskState
from orchestrator.providers.base import (
    AgentEventKind,
    ErrorCategory,
    ExecutionRequest,
    ExecutionResult,
    ProviderAdapter,
)
from orchestrator.scheduler.ports import RouteChoice, RouteUnavailable, UsageSink
from orchestrator.security.redact import redact
from orchestrator.services import agents as agent_svc
from orchestrator.services.context import AppContext
from orchestrator.services.routing import RoutingService, TaskSpec
from orchestrator.util import new_id, now_iso, truncate

log = logging.getLogger(__name__)

CALL_TIMEOUT_SECONDS = 600.0
OUTPUT_EVENT_LIMIT = 4000


@dataclass
class CallOutcome:
    result: ExecutionResult
    run_id: str
    provider: str
    model: Optional[str]
    route: Optional[RouteChoice] = None
    error: Optional[str] = None  # set when the call never reached a provider (no candidate, no adapter)
    unavailable: Optional[RouteUnavailable] = None
    detail: dict[str, Any] = field(default_factory=dict)


class CallRunner:
    def __init__(
        self,
        ctx: AppContext,
        providers: Mapping[str, ProviderAdapter],
        routing: Optional[RoutingService],
        agents: agent_svc.AgentService,
        usage: Optional[UsageSink] = None,
        *,
        timeout_seconds: float = CALL_TIMEOUT_SECONDS,
    ):
        self.ctx = ctx
        self.providers = providers
        self.routing = routing
        self.agents = agents
        self.usage = usage
        self.timeout_seconds = timeout_seconds
        self._live: dict[str, tuple[ProviderAdapter, str]] = {}

    # ------------------------------------------------------------------ routing
    async def route(
        self,
        phase: RunPhase,
        *,
        role: str,
        pin: Optional[tuple[str, Optional[str]]],
        task: Optional[Task] = None,
        exclude: frozenset[str] = frozenset(),
    ) -> RouteChoice:
        """RouteUnavailable when nothing suitable exists (never an unsuitable pick)."""
        if self.routing is not None:
            return await self.routing.route_call(
                TaskSpec("leadership call", "", "planning"), phase.value, role=role, pin=pin, task=task, exclude=exclude
            )
        # No routing service (an injected custom router): honour the pin directly against the adapters.
        if pin is None or pin[0] not in self.providers or pin[0] in exclude:
            raise RouteUnavailable("blocked", f"no routing service and no usable pinned identity for role {role}")
        return RouteChoice(provider=pin[0], model=pin[1], reasons=["pinned identity (no routing service)"], rule_ids=["pin"])

    # ------------------------------------------------------------------ the call
    async def call(
        self,
        phase: RunPhase,
        prompt: str,
        *,
        schema: dict[str, Any],
        role: str,
        purpose: str,
        cwd: str,
        task: Optional[Task] = None,
        project_id: Optional[str] = None,
        pin: Optional[tuple[str, Optional[str]]] = None,
        exclude: frozenset[str] = frozenset(),
        route: Optional[RouteChoice] = None,
    ) -> CallOutcome:
        """One recorded read-only provider call. Never raises for provider/routing failures: the outcome
        carries them (result.success False, `unavailable` for routing)."""
        run_id = new_id("run")
        if route is None:
            try:
                route = await self.route(phase, role=role, pin=pin, task=task, exclude=exclude)
            except RouteUnavailable as exc:
                await self.ctx.bus.emit(
                    ev.ROUTER_DECISION, project_id=project_id, task_id=task.id if task else None,
                    payload={"phase": phase.value, "provider": None, "model": None, "outcome": exc.outcome,
                             "reasons": [exc.reason], "purpose": purpose, "detail": exc.detail},
                )
                res = ExecutionResult(success=False, error_category=ErrorCategory.QUOTA_EXHAUSTED if exc.outcome == "wait_for_quota" else ErrorCategory.INVALID_REQUEST,
                                      error_message=exc.reason[:500])
                return CallOutcome(result=res, run_id="", provider="", model=None, error=exc.reason, unavailable=exc)
        adapter = self.providers.get(route.provider) if route.provider in self.providers else None
        if adapter is None:
            res = ExecutionResult(success=False, error_category=ErrorCategory.INVALID_REQUEST, error_message=f"provider {route.provider!r} is not registered")
            return CallOutcome(result=res, run_id="", provider=route.provider, model=route.model, route=route, error=res.error_message)

        seat = agent_svc.leadership_seat(phase)
        activity = agent_svc.activity_for(task.state if task else TaskState.CREATED, phase=phase)
        task_id = task.id if task else None
        key = f"call:{purpose}:{run_id}"
        ts = now_iso()

        def start(tx: Tx) -> None:
            runs_repo.insert(
                tx,
                {
                    "id": run_id, "task_id": task_id, "phase": phase.value, "agent_id": seat, "provider": route.provider,
                    "model": route.model, "status": RunStatus.STARTING.value, "usage": {}, "started_at": ts,
                    "heartbeat_at": ts, "idempotency_key": key,
                },
            )
            self.ctx.bus.append(
                tx, ev.ROUTER_DECISION, project_id=project_id, task_id=task_id, run_id=run_id,
                payload={"phase": phase.value, "provider": route.provider, "model": route.model, "reasons": route.reasons, "purpose": purpose},
            )
            self.ctx.bus.append(
                tx, ev.RUN_STARTED, project_id=project_id, task_id=task_id, run_id=run_id, agent_id=seat,
                payload={"phase": phase.value, "provider": route.provider, "model": route.model, "idempotency_key": key, "purpose": purpose},
            )
            if seat:
                agent_svc.set_activity_tx(
                    self.ctx, tx, seat, activity, live=True, task_id=task_id, run_id=run_id,
                    operation=f"{purpose} via {route.provider}", provider=route.provider, model=route.model, project_id=project_id,
                )

        await self.ctx.db.write(start)
        policies = await self.ctx.policies()
        request = ExecutionRequest(
            run_id=run_id, prompt=prompt, cwd=cwd, model=route.model,
            permission=policies.worker_permission(phase.value),  # type: ignore[arg-type]
            json_schema=schema, timeout_seconds=self.timeout_seconds,
        )
        self._live[run_id] = (adapter, run_id)
        result: Optional[ExecutionResult] = None
        try:
            result = await asyncio.wait_for(self._consume(adapter, request, route, run_id, project_id, task_id), self.timeout_seconds + 30)
        except asyncio.TimeoutError:
            try:
                await adapter.cancel(run_id)
            except Exception:  # noqa: BLE001
                log.exception("cancel after timeout failed")
            result = ExecutionResult(success=False, error_category=ErrorCategory.TIMEOUT, error_message=f"call exceeded {self.timeout_seconds:.0f}s")
        except asyncio.CancelledError:
            try:
                await asyncio.shield(adapter.cancel(run_id))
            except Exception:  # noqa: BLE001
                pass
            await asyncio.shield(self._finish(run_id, ExecutionResult(success=False, error_category=ErrorCategory.CANCELLED, error_message="cancelled"), seat, project_id, task_id))
            raise
        finally:
            self._live.pop(run_id, None)
        if result.model is None:
            result.model = route.model
        if self.usage is not None:
            try:
                await self.usage.on_result(route.provider, result, resets_at=result.usage.get("_resets_at"))
            except Exception:  # noqa: BLE001 - usage bookkeeping must never break a call
                log.exception("usage sink (result) failed")
        await self._finish(run_id, result, seat, project_id, task_id)
        return CallOutcome(result=result, run_id=run_id, provider=route.provider, model=result.model or route.model, route=route)

    async def _consume(
        self, adapter: ProviderAdapter, request: ExecutionRequest, route: RouteChoice, run_id: str,
        project_id: Optional[str], task_id: Optional[str],
    ) -> ExecutionResult:
        usage: dict[str, Any] = {}
        result: Optional[ExecutionResult] = None
        pid_recorded = False
        gen = adapter.execute(request)
        try:
            async for event in gen:
                if not pid_recorded:
                    pid = adapter.pid_for(run_id)
                    if pid:
                        pid_recorded = True
                        await self.ctx.db.write(lambda tx, p=pid: runs_repo.update(tx, run_id, {"pid": p, "status": RunStatus.RUNNING.value}))
                kind, data = event.kind, event.data
                if kind == AgentEventKind.RESULT:
                    result = ExecutionResult.model_validate(data)
                    break
                if kind == AgentEventKind.SESSION:
                    await self.ctx.db.write(
                        lambda tx: runs_repo.update(tx, run_id, {"session_id": data.get("session_id"), "status": RunStatus.RUNNING.value})
                    )
                elif kind == AgentEventKind.TEXT:
                    text = truncate(redact(str(data.get("text", ""))), OUTPUT_EVENT_LIMIT)
                    await self.ctx.bus.emit(ev.RUN_OUTPUT, project_id=project_id, task_id=task_id, run_id=run_id, payload={"text": text})
                elif kind == AgentEventKind.USAGE:
                    for k, v in data.items():
                        if isinstance(v, (int, float)) and not isinstance(v, bool) and isinstance(usage.get(k, 0), (int, float)):
                            usage[k] = usage.get(k, 0) + v
                    await self.ctx.bus.emit(ev.RUN_USAGE, project_id=project_id, task_id=task_id, run_id=run_id, payload={"delta": dict(data), "total": dict(usage)})
                    await self._sink_event(route, event)
                elif kind == AgentEventKind.RATE_LIMIT:
                    usage["rate_limit"] = dict(data)
                    await self._sink_event(route, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - adapter contract violation must not kill the caller
            log.exception("provider adapter raised")
            result = ExecutionResult(success=False, error_category=ErrorCategory.CRASHED, error_message=f"adapter error: {exc!r}"[:500])
        finally:
            try:
                await asyncio.wait_for(gen.aclose(), 5)
            except Exception:  # noqa: BLE001
                pass
        if result is None:
            result = ExecutionResult(success=False, error_category=ErrorCategory.CRASHED, error_message="provider stream ended without a RESULT event")
        merged = dict(usage)
        merged.update(result.usage or {})
        result.usage = merged
        return result

    async def _sink_event(self, route: RouteChoice, event: Any) -> None:
        if self.usage is None:
            return
        try:
            await self.usage.on_event(route.provider, route.model, event)
        except Exception:  # noqa: BLE001
            log.exception("usage sink (event) failed")

    async def _finish(self, run_id: str, result: ExecutionResult, seat: Optional[str], project_id: Optional[str], task_id: Optional[str]) -> None:
        status = RunStatus.CANCELLED if result.error_category == ErrorCategory.CANCELLED else (RunStatus.SUCCEEDED if result.success else RunStatus.FAILED)
        summary = truncate(redact(result.final_text or ""), 2000, keep="head")

        def fn(tx: Tx) -> None:
            fields: dict[str, Any] = {
                "status": status.value,
                "error_category": result.error_category.value if result.error_category else None,
                "error_message": redact(result.error_message)[:2000] if result.error_message else None,
                "exit_code": result.exit_code, "usage": result.usage, "finished_at": now_iso(),
            }
            if result.session_id:
                fields["session_id"] = result.session_id
            if result.model:
                fields["model"] = result.model
            runs_repo.update(tx, run_id, fields)
            self.ctx.bus.append(
                tx, ev.RUN_FINISHED, project_id=project_id, task_id=task_id, run_id=run_id, agent_id=seat,
                payload={
                    "status": status.value, "success": result.success, "error_category": fields["error_category"],
                    "error_message": fields["error_message"], "exit_code": result.exit_code, "usage": result.usage,
                    "duration_seconds": result.duration_seconds, "summary": summary,
                },
            )
            if seat:
                a = records.agent_get(tx, seat)
                if a is not None and a.current_run_id == run_id:  # another live call may own the seat now
                    agent_svc.set_activity_tx(self.ctx, tx, seat, AgentActivity.IDLE, project_id=project_id)

        await self.ctx.db.write(fn)

    async def cancel_all(self) -> None:
        for adapter, run_id in list(self._live.values()):
            try:
                await adapter.cancel(run_id)
            except Exception:  # noqa: BLE001
                log.exception("cancel failed for %s", run_id)
