"""Provider routes: GET /api/providers, POST /api/providers/refresh, GET /api/models, GET /api/usage."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, ConfigDict

from orchestrator.api.deps import AUTH, get_rt
from orchestrator.api.runtime import Runtime
from orchestrator.errors import NotFound
from orchestrator.services.providers import ProviderService
from orchestrator.services.usage import UsageMonitor, state_json

router = APIRouter(prefix="/api", tags=["providers"], dependencies=AUTH)


class RefreshBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Optional[str] = None


def _svc(rt: Runtime) -> ProviderService:
    return rt.extras["providers"]


def _usage(rt: Runtime) -> UsageMonitor:
    return rt.extras["usage"]


async def _provider_list(rt: Runtime) -> list[dict[str, Any]]:
    svc = _svc(rt)
    statuses = svc.statuses()
    states = {s.provider: s for s in await _usage(rt).states(svc.registry.names())}
    models = svc.registry.models()
    out: list[dict[str, Any]] = []
    for name in svc.registry.names():
        st = statuses.get(name)
        usage = states.get(name)
        entry: dict[str, Any] = {
            "provider": name,
            "detected": st is not None,
            "installed": bool(st.installed) if st is not None else False,
            "version": st.version if st else None,
            "executable": st.executable if st else None,
            "authenticated": st.authenticated if st else None,
            "auth_detail": st.auth_detail if st else None,
            "error": st.error if st else "detection pending",
            "capabilities": st.capabilities.model_dump(mode="json") if st else None,
            "billing_class": svc.billing_class(name),
            "checked_at": svc.refreshed_at.get(name),
            "model_count": len(models.get(name, [])),
            "usage": state_json(usage) if usage is not None else None,
        }
        out.append(entry)
    return out


@router.get("/providers")
async def list_providers(rt: Runtime = Depends(get_rt)) -> list[dict[str, Any]]:
    return await _provider_list(rt)


@router.post("/providers/refresh")
async def refresh_providers(body: Optional[RefreshBody] = Body(default=None), rt: Runtime = Depends(get_rt)) -> list[dict[str, Any]]:
    name = body.provider if body else None
    try:
        await _svc(rt).refresh(name)
    except KeyError:
        raise NotFound(f"unknown provider {name!r}") from None
    return await _provider_list(rt)


@router.get("/models")
async def list_models(rt: Runtime = Depends(get_rt)) -> list[dict[str, Any]]:
    svc = _svc(rt)
    cap = svc.capability_registry()
    statuses = svc.statuses()
    discovered: dict[str, dict[str, Any]] = {}
    for infos in svc.registry.models().values():
        for m in infos:
            entry = cap.get(m.provider, m.model_id)
            discovered[f"{m.provider}/{entry.model_id if entry else m.model_id}"] = m
    out = []
    for e in cap.entries():
        info = discovered.get(f"{e.provider}/{e.model_id}")
        st = statuses.get(e.provider)
        out.append({
            "provider": e.provider,
            "model_id": e.model_id,
            "alias": e.alias,
            "display_name": info.display_name if info else None,
            "tier": e.tier,
            "tier_source": e.tier_source,
            "capabilities": sorted(e.capabilities),
            "listed_capabilities": sorted(e.listed_capabilities),
            "billing_class": e.billing_class,
            "verification": e.verification,
            "source": e.source,
            "discovered": info is not None,
            "provider_installed": bool(st.installed) if st is not None else False,
        })
    return out


@router.get("/usage")
async def get_usage(rt: Runtime = Depends(get_rt)) -> list[dict[str, Any]]:
    return await _usage(rt).report(_svc(rt).registry.names())
