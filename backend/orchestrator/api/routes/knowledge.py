"""Knowledge routes (ARCHITECTURE §6, §11).

Register with: create_app(extra_routers=[knowledge.router], on_startup=[orchestrator.knowledge.startup]).
`POST /index` answers 202 {"job_id", "status": "running"} and indexes in the background (event `knowledge.indexed`
carries the report; `GET /index/{job_id}` returns it too). Pass `"wait": true` to get the report inline (200).
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.api.deps import AUTH, get_rt
from orchestrator.api.runtime import Runtime
from orchestrator.errors import Conflict, NotFound, OrchestratorError
from orchestrator.knowledge.retrieval import KINDS
from orchestrator.knowledge.service import KnowledgeService
from orchestrator.knowledge.sources import GlobError

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"], dependencies=AUTH)

Kind = Literal["requirement", "adr", "doc", "task", "error_resolution", "code_meta", "skill", "model_history"]
Source = Literal["docs", "tasks", "errors", "code", "skills", "decisions", "models"]


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=1000)
    project_id: Optional[str] = None
    kinds: Optional[list[Kind]] = None
    limit: int = Field(default=8, ge=1, le=50)


class IndexBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: Optional[str] = None
    sources: Optional[list[Source]] = None
    full: bool = False
    globs: Optional[list[str]] = Field(default=None, max_length=30)
    wait: bool = False


class BadRequest(OrchestratorError):
    code = "bad_request"
    status = 400


def svc(rt: Runtime = Depends(get_rt)) -> KnowledgeService:
    s = rt.extras.get("knowledge")
    if s is None:
        raise OrchestratorError("knowledge service is not started (register orchestrator.knowledge.startup)")
    return s


async def _require_project(s: KnowledgeService, project_id: Optional[str]) -> None:
    if project_id and await s.ctx.db.one("SELECT 1 FROM projects WHERE id = ?", (project_id,)) is None:
        raise NotFound(f"project {project_id} not found")


@router.post("/search")
async def search(body: SearchBody, s: KnowledgeService = Depends(svc)) -> dict[str, Any]:
    await _require_project(s, body.project_id)
    return await s.search(body.query, project_id=body.project_id, kinds=body.kinds, limit=body.limit)


@router.post("/index")
async def index(body: IndexBody, response: Response, s: KnowledgeService = Depends(svc)) -> dict[str, Any]:
    await _require_project(s, body.project_id)
    if body.globs:
        from orchestrator.knowledge.sources import validate_globs

        try:
            validate_globs(body.globs)
        except GlobError as exc:
            raise BadRequest(str(exc)) from exc
    kwargs = {"project_id": body.project_id, "sources": body.sources, "full": body.full, "globs": body.globs}
    if body.wait:
        return (await s.index(**kwargs)).as_dict()
    response.status_code = 202
    return {"job_id": s.start_job(**kwargs), "status": "running"}


@router.get("/index/{job_id}")
async def index_job(job_id: str, s: KnowledgeService = Depends(svc)) -> dict[str, Any]:
    job = s.jobs.get(job_id)
    if job is None:
        raise NotFound(f"index job {job_id} not found")
    return job


@router.get("/docs")
async def list_docs(project_id: Optional[str] = None, kind: Optional[Kind] = None, limit: int = Query(50, ge=1, le=500),
                    offset: int = Query(0, ge=0), s: KnowledgeService = Depends(svc)) -> dict[str, Any]:
    return await s.list_docs(project_id=project_id, kind=kind, limit=limit, offset=offset)


@router.get("/docs/{doc_id}")
async def get_doc(doc_id: str, s: KnowledgeService = Depends(svc)) -> dict[str, Any]:
    doc = await s.get_doc(doc_id)
    if doc is None:
        raise NotFound(f"knowledge doc {doc_id} not found")
    return doc


@router.delete("/docs/{doc_id}", status_code=204)
async def delete_doc(doc_id: str, s: KnowledgeService = Depends(svc)) -> Response:
    if not await s.delete_doc(doc_id):
        raise NotFound(f"knowledge doc {doc_id} not found")
    return Response(status_code=204)


@router.get("/stats")
async def stats(s: KnowledgeService = Depends(svc)) -> dict[str, Any]:
    return await s.stats()
