"""Analytics routes (ARCHITECTURE §6, §11).

Register with: create_app(extra_routers=[analytics.router], on_startup=[orchestrator.analytics.startup]).
Every metric is {value, exactness, source, n}; no data => exactness "missing", value null.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.analytics import AnalyticsService
from orchestrator.analytics import adaptive as adaptive_mod
from orchestrator.analytics import metrics
from orchestrator.api.deps import AUTH, get_rt
from orchestrator.api.runtime import Runtime
from orchestrator.errors import OrchestratorError, ValidationFailed

router = APIRouter(prefix="/api/analytics", tags=["analytics"], dependencies=AUTH)


class AdaptiveSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: Optional[bool] = None
    min_samples: Optional[int] = Field(default=None, ge=adaptive_mod.BOUNDS["min_samples"][0], le=adaptive_mod.BOUNDS["min_samples"][1])
    max_adjustment: Optional[float] = Field(default=None, ge=adaptive_mod.BOUNDS["max_adjustment"][0], le=adaptive_mod.BOUNDS["max_adjustment"][1])


def svc(rt: Runtime = Depends(get_rt)) -> AnalyticsService:
    s = rt.extras.get("analytics")
    if s is None:
        raise OrchestratorError("analytics service is not started (register orchestrator.analytics.startup)")
    return s


def scope_of(project_id: Optional[str] = None, provider: Optional[str] = None, model: Optional[str] = None, task_type: Optional[str] = None,
             from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None) -> metrics.Scope:
    try:
        return metrics.Scope.make(project_id, provider, model, task_type, from_, to)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


@router.get("/summary")
async def summary(scope: metrics.Scope = Depends(scope_of), s: AnalyticsService = Depends(svc)) -> dict[str, Any]:
    return await s.summary(scope)


@router.get("/models")
async def models(provider: Optional[str] = None, model: Optional[str] = None, task_type: Optional[str] = None,
                 s: AnalyticsService = Depends(svc)) -> dict[str, Any]:
    return {"models": await s.models(provider=provider, model=model, task_type=task_type),
            "definition": "verified_runs/successes: db.telemetry.history_stats (execute/repair runs of tasks that later passed verification)"}


@router.get("/routing")
async def routing(s: AnalyticsService = Depends(svc)) -> dict[str, Any]:
    return await s.routing_report()


@router.get("/timeseries")
async def timeseries(metric: str = Query(...), bucket: Literal["day"] = "day", scope: metrics.Scope = Depends(scope_of),
                     s: AnalyticsService = Depends(svc)) -> dict[str, Any]:
    try:
        return await s.timeseries(scope, metric)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


@router.get("/agents")
async def agents(scope: metrics.Scope = Depends(scope_of), s: AnalyticsService = Depends(svc)) -> dict[str, Any]:
    return {"agents": await s.agents(scope)}


@router.get("/adaptive-settings")
async def get_adaptive_settings(s: AnalyticsService = Depends(svc)) -> dict[str, Any]:
    return await s.get_adaptive_settings()


@router.put("/adaptive-settings")
async def put_adaptive_settings(body: AdaptiveSettingsBody, s: AnalyticsService = Depends(svc)) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise ValidationFailed("provide at least one of enabled, min_samples, max_adjustment")
    return await s.put_adaptive_settings(patch)
