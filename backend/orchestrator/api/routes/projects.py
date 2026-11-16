"""Project routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from orchestrator.api.deps import AUTH, get_rt
from orchestrator.api.runtime import Runtime
from orchestrator.domain.models import Project, ProjectCreate, ProjectUpdate

router = APIRouter(prefix="/api/projects", tags=["projects"], dependencies=AUTH)


@router.get("")
async def list_projects(rt: Runtime = Depends(get_rt)) -> list[Project]:
    return await rt.projects.list()


@router.post("", status_code=201)
async def create_project(body: ProjectCreate, rt: Runtime = Depends(get_rt)) -> Project:
    return await rt.projects.create(body)


@router.get("/{project_id}")
async def get_project(project_id: str, rt: Runtime = Depends(get_rt)) -> Project:
    return await rt.projects.get(project_id)


@router.patch("/{project_id}")
async def update_project(project_id: str, body: ProjectUpdate, rt: Runtime = Depends(get_rt)) -> Project:
    return await rt.projects.update(project_id, body)


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: str, rt: Runtime = Depends(get_rt)) -> Response:
    await rt.projects.delete(project_id)
    return Response(status_code=204)
