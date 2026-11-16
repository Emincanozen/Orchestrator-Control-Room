"""Leadership routes: settings, safe leader switch, advisor consults, decision records."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.api.deps import AUTH, get_rt
from orchestrator.api.runtime import Runtime
from orchestrator.domain.models import Decision
from orchestrator.errors import OrchestratorError
from orchestrator.leader import LeaderStack
from orchestrator.leader.advisor import AdvisorRefused
from orchestrator.leader.service import LeadershipUpdate

router = APIRouter(prefix="/api", tags=["leadership"], dependencies=AUTH)


def _stack(rt: Runtime) -> LeaderStack:
    return rt.extras["leader"]


class SwitchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    skip_validation: bool = False  # local/mock providers only; recorded in leader.changed


class ConsultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: Optional[str] = None
    problem: str = Field(min_length=1, max_length=20_000)
    options: list[str] = Field(default_factory=list, max_length=8)
    constraints: list[str] = Field(default_factory=list, max_length=8)


class LeaderSwitchFailed(OrchestratorError):
    code = "leader_switch_failed"
    status = 409


@router.get("/leadership")
async def get_leadership(rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    st = _stack(rt)
    await st.leadership.ensure_default_leader()  # first start: record the deterministic default
    return await st.leadership.view(rt.scheduler)


@router.put("/leadership")
async def put_leadership(body: LeadershipUpdate, rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    st = _stack(rt)
    await st.leadership.update(body)
    rt.scheduler.wake()
    return await st.leadership.view(rt.scheduler)


@router.post("/leadership/switch")
async def switch_leader(body: SwitchBody, rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    st = _stack(rt)
    assert st.switch is not None
    result = await st.switch.switch(body.provider, body.model, skip_validation=body.skip_validation, reason="manual switch (API)")
    if result["status"] != "switched":
        raise LeaderSwitchFailed(
            f"the new leader could not validate the coordination state; rolled back to {result['from']['provider']}/{result['from']['model']}: {result['reason']}",
            details=result,
        )
    return result


@router.post("/advisor/consult")
async def consult(body: ConsultBody, rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    try:
        return await _stack(rt).advisor.consult(
            trigger="user", problem=body.problem, task_id=body.task_id, options=body.options, constraints=body.constraints
        )
    except AdvisorRefused:
        raise  # mapped by the error handler: 409 advisor_refused / 429 advisor_budget_exhausted, details.reason


@router.get("/decisions")
async def list_decisions(
    project_id: Optional[str] = None,
    task_id: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=200),
    rt: Runtime = Depends(get_rt),
) -> list[Decision]:
    return await _stack(rt).decisions.list(project_id=project_id, task_id=task_id, limit=limit)


@router.post("/decisions/{decision_id}/export")
async def export_decision(decision_id: str, rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    """Append the decision as an ADR-style entry to <project>/docs/DECISIONS.md. Only on explicit request."""
    return await _stack(rt).decisions.export(decision_id)
