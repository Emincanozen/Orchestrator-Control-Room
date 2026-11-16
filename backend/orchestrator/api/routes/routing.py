"""Routing routes: decisions history, side-effect-free preview, user preferences."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.api.deps import AUTH, get_rt
from orchestrator.api.runtime import Runtime
from orchestrator.db import records, telemetry
from orchestrator.db.database import Tx
from orchestrator.domain.models import RoutingDecision
from orchestrator.errors import ValidationFailed
from orchestrator.services.routing import PREFERENCE_FIELDS, PREFERENCES_KEY, RoutingService, TaskSpec

router = APIRouter(prefix="/api/routing", tags=["routing"], dependencies=AUTH)


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=100_000)
    task_type: Optional[str] = None
    role: Optional[str] = None
    phase: Literal["plan", "execute", "repair", "review", "advise"] = "execute"


class PreferencesBody(BaseModel):
    """Only the fields sent are changed; an explicit null removes that override (back to routing.yaml)."""

    model_config = ConfigDict(extra="forbid")
    provider_order: Optional[list[str]] = None
    disabled_providers: Optional[list[str]] = None
    disabled_models: Optional[list[str]] = None


def _svc(rt: Runtime) -> RoutingService:
    return rt.extras["routing"]


@router.get("/decisions")
async def list_decisions(
    task_id: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
    rt: Runtime = Depends(get_rt),
) -> list[RoutingDecision]:
    return await rt.ctx.db.read(lambda tx: telemetry.routing_recent(tx, task_id, limit))


@router.post("/preview")
async def preview(body: PreviewBody, rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    """Classification + the decision the router would take now. No run, no row, no event."""
    return await _svc(rt).preview(TaskSpec(body.title, body.description, body.task_type, body.role), body.phase)


def _prefs_view(svc: RoutingService, tx: Tx) -> dict[str, Any]:
    overrides = records.settings_all(tx).get(PREFERENCES_KEY)
    overrides = overrides if isinstance(overrides, dict) else {}
    return {
        **svc.effective_preferences_tx(tx),
        "defaults": svc.default_preferences(),
        "overridden": sorted(k for k in PREFERENCE_FIELDS if k in overrides),
    }


@router.get("/preferences")
async def get_preferences(rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    svc = _svc(rt)
    return await rt.ctx.db.read(lambda tx: _prefs_view(svc, tx))


def _clean(name: str, values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        if not isinstance(v, str) or not v.strip():
            raise ValidationFailed(f"{name} must contain non-empty strings")
        if v.strip() not in out:
            out.append(v.strip())
    return out


@router.put("/preferences")
async def put_preferences(body: PreferencesBody, rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    svc = _svc(rt)
    sent = body.model_dump(exclude_unset=True)

    def fn(tx: Tx) -> dict[str, Any]:
        current = records.settings_all(tx).get(PREFERENCES_KEY)
        merged = dict(current) if isinstance(current, dict) else {}
        for key, value in sent.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = _clean(key, value)
        if merged:
            records.setting_put(tx, PREFERENCES_KEY, merged)
        else:
            records.setting_delete(tx, PREFERENCES_KEY)
        return _prefs_view(svc, tx)

    view = await rt.ctx.db.write(fn)
    rt.scheduler.wake()
    return view
