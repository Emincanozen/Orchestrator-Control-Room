"""Objective routes: the leader decomposes a user goal into dependency-aware tasks."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.api.deps import AUTH, get_rt
from orchestrator.api.runtime import Runtime
from orchestrator.errors import NotFound

router = APIRouter(prefix="/api", tags=["objectives"], dependencies=AUTH)


class ObjectiveCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=20_000)
    autostart: bool = False  # queue the created tasks immediately (dependencies still gate execution)
    wait: bool = False  # block until the decomposition finished (default: returns `planning`, poll GET)


@router.post("/projects/{project_id}/objectives", status_code=201)
async def create_objective(project_id: str, body: ObjectiveCreate, rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    view = await rt.extras["leader"].objectives.create(project_id, body.text, autostart=body.autostart, wait=body.wait)
    if body.autostart:
        rt.scheduler.wake()
    return view


@router.get("/projects/{project_id}/objectives")
async def list_objectives(project_id: str, rt: Runtime = Depends(get_rt)) -> list[dict[str, Any]]:
    await rt.projects.get(project_id)
    return await rt.extras["leader"].objectives.list(project_id)


@router.get("/objectives/{objective_id}")
async def get_objective(objective_id: str, rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    try:
        return await rt.extras["leader"].objectives.get(objective_id)
    except NotFound:
        raise
