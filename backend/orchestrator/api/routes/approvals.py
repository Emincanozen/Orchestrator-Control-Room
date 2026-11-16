"""Approval routes."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends

from orchestrator.api.deps import AUTH, get_rt
from orchestrator.api.runtime import Runtime
from orchestrator.domain.models import Approval, ApprovalResolve

router = APIRouter(prefix="/api/approvals", tags=["approvals"], dependencies=AUTH)


@router.get("")
async def list_approvals(
    status: Optional[Literal["pending", "approved", "denied"]] = None,
    task_id: Optional[str] = None,
    rt: Runtime = Depends(get_rt),
) -> list[Approval]:
    return await rt.approvals.list(status, task_id)


@router.post("/{approval_id}/resolve")
async def resolve_approval(approval_id: str, body: ApprovalResolve, rt: Runtime = Depends(get_rt)) -> Approval:
    approval = await rt.approvals.resolve(approval_id, body.approve, note=body.note, by="user")
    rt.scheduler.wake()
    return approval
