"""Skills & MCP Manager routes (ARCHITECTURE §6).

Register with: create_app(extra_routers=[skills.router], on_startup=[orchestrator.skills.startup]).
Action endpoints answer 200 with {"outcome": {...}, "skill": <detail>}; activation that needs an approval answers
202 with {"outcome": {status: "approval_required", approval_id, ...}, "skill": <detail>}.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.api.deps import AUTH, get_rt
from orchestrator.api.runtime import Runtime
from orchestrator.skills import views
from orchestrator.skills.errors import SkillError
from orchestrator.skills.manager import SkillManager
from orchestrator.skills import store

router = APIRouter(prefix="/api/skills", tags=["skills"], dependencies=AUTH)


class ActivateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    scope: Literal["project"] = "project"
    targets: list[Literal["claude", "codex"]] = Field(default_factory=lambda: ["claude"])


class DeactivateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str


class UpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: Optional[str] = Field(default=None, max_length=200)


class SkillPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_url: str = Field(min_length=8, max_length=500)


def manager(rt: Runtime = Depends(get_rt)) -> SkillManager:
    m = rt.extras.get("skills")
    if m is None:
        raise SkillError("skills manager is not started (register orchestrator.skills.startup)", code="skills_unavailable")
    return m


async def _detail(m: SkillManager, skill_id: str) -> dict[str, Any]:
    rows = await m.all_rows()
    row = next((r for r in rows if r["id"] == skill_id), None)
    events = await m.ctx.db.read(lambda tx: store.events_tx(tx, skill_id))
    assert row is not None
    return views.detail(m, row, m.warnings_for(rows).get(skill_id, []), events)


@router.get("")
async def list_skills(
    q: Optional[str] = None,
    category: Optional[str] = None,
    kind: Optional[str] = None,
    state: Optional[str] = None,
    project_id: Optional[str] = None,
    m: SkillManager = Depends(manager),
) -> list[dict[str, Any]]:
    rows = await m.all_rows()
    warn = m.warnings_for(rows)
    needle = (q or "").strip().lower()
    out = []
    for r in rows:
        if category and r["category"] != category:
            continue
        if kind and r["kind"] != kind:
            continue
        if state and r["state"] != state:
            continue
        if needle:
            label = (r["install_method"].get("catalog", {}).get("user_label") or "")
            if needle not in f"{r['id']} {r['name']} {r['description']} {label}".lower():
                continue
        out.append(views.summary(m, r, warn.get(r["id"], []), project_id))
    return out


@router.get("/{skill_id}")
async def get_skill(skill_id: str, m: SkillManager = Depends(manager)) -> dict[str, Any]:
    await m.get_row(skill_id)
    return await _detail(m, skill_id)


@router.get("/{skill_id}/content")
async def skill_content(skill_id: str, m: SkillManager = Depends(manager)) -> dict[str, Any]:
    return await m.content(skill_id)


@router.patch("/{skill_id}")
async def patch_skill(skill_id: str, body: SkillPatch, m: SkillManager = Depends(manager)) -> dict[str, Any]:
    outcome = await m.set_source(skill_id, body.source_url)
    return {"outcome": outcome, "skill": await _detail(m, skill_id)}


@router.post("/{skill_id}/install")
async def install_skill(skill_id: str, m: SkillManager = Depends(manager)) -> dict[str, Any]:
    outcome = await m.install(skill_id)
    return {"outcome": outcome, "skill": await _detail(m, skill_id)}


@router.post("/{skill_id}/activate")
async def activate_skill(skill_id: str, body: ActivateBody, response: Response, m: SkillManager = Depends(manager)) -> dict[str, Any]:
    status, outcome = await m.activate(skill_id, body.project_id, scope=body.scope, targets=body.targets)
    response.status_code = status
    return {"outcome": outcome, "skill": await _detail(m, skill_id)}


@router.post("/{skill_id}/deactivate")
async def deactivate_skill(skill_id: str, body: DeactivateBody, m: SkillManager = Depends(manager)) -> dict[str, Any]:
    outcome = await m.deactivate(skill_id, body.project_id)
    return {"outcome": outcome, "skill": await _detail(m, skill_id)}


@router.post("/{skill_id}/update")
async def update_skill(skill_id: str, body: Optional[UpdateBody] = None, m: SkillManager = Depends(manager)) -> dict[str, Any]:
    outcome = await m.update(skill_id, body.ref if body else None)
    return {"outcome": outcome, "skill": await _detail(m, skill_id)}


@router.post("/{skill_id}/rollback")
async def rollback_skill(skill_id: str, m: SkillManager = Depends(manager)) -> dict[str, Any]:
    outcome = await m.rollback(skill_id)
    return {"outcome": outcome, "skill": await _detail(m, skill_id)}
