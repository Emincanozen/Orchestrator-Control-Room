"""JSON shapes returned by the skills API. Metadata only: never SKILL.md bodies (see GET /{id}/content)."""

from __future__ import annotations

from typing import Any, Optional

from orchestrator.skills import compat
from orchestrator.skills.manager import SkillManager


def summary(m: SkillManager, s: dict[str, Any], warnings: list[dict[str, Any]], project_id: Optional[str] = None) -> dict[str, Any]:
    ok, why = m.installable(s)
    cat = s["install_method"].get("catalog", {})
    local = s["install_method"].get("local", {})
    sec = s["security"]
    out: dict[str, Any] = {
        "id": s["id"],
        "name": s["name"],
        "user_label": cat.get("user_label"),
        "category": s["category"],
        "kind": s["kind"],
        "description": s["description"],
        "state": s["state"],
        "source_url": s["source_url"],
        "source_verified": s["source_verified"],
        "source": local.get("source", "catalog"),
        "installable": ok,
        "installable_reason": why,
        "activation": m.activation_info(s),
        "pinned_ref": s["pinned_ref"],
        "installed_ref": s["installed_ref"],
        "previous_ref": s["previous_ref"],
        "compat": s["compat"],
        "risk": sec.get("risk"),
        "requires_credentials": bool(sec.get("requires_credentials")),
        "has_uncertainty": bool(s["uncertainty"]),
        "last_error": local.get("last_error"),
        "incompatible_reasons": local.get("incompatible") or [],
        "active_projects": [a["project_id"] for a in s["project_scopes"]],
        "warnings": warnings,
        "updated_at": s["updated_at"],
    }
    if project_id:
        act = next((a for a in s["project_scopes"] if a["project_id"] == project_id), None)
        out["project_activation"] = (
            {"active": True, "ref": act["ref"], "name": act["name"], "mode": act["mode"], "activated_at": act["activated_at"]} if act else {"active": False}
        )
    return out


def detail(m: SkillManager, s: dict[str, Any], warnings: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    cat = s["install_method"].get("catalog", {})
    local = s["install_method"].get("local", {})
    out = summary(m, s, warnings)
    out.update(
        {
            "uncertainty": s["uncertainty"],
            "candidates": cat.get("candidates", []),
            "license": cat.get("license"),
            "maintenance": cat.get("maintenance", {}),
            "default_branch": cat.get("default_branch"),
            "paid_service": cat.get("paid_service"),
            "priority_candidate": cat.get("priority_candidate", False),
            "duplicates_of": cat.get("duplicates_of"),
            "install_method": {k: v for k, v in s["install_method"].items() if k not in ("catalog", "local")},
            "dependencies": s["dependencies"],
            "permissions": s["permissions"],
            "security": s["security"],
            "local_path": s["local_path"],
            "archives": local.get("archives", []),
            "pending_reactivation": local.get("pending_reactivation", []),
            "activations": [{k: v for k, v in a.items() if k != "artifacts"} | {"artifacts": [_art(x) for x in a.get("artifacts", [])]} for a in s["project_scopes"]],
            "events": events,
            "instructions": m.instructions(s),
            "codex": compat.codex_support(m.codex_agents_skills),
        }
    )
    return out


def _art(a: dict[str, Any]) -> dict[str, Any]:
    if a["type"] == "skill_dir":
        return {"type": "skill_dir", "target": a.get("target"), "path": a["path"], "file_count": len(a.get("files", []))}
    return {"type": "mcp_entry", "path": a["path"], "server": a["server"], "created_file": a.get("created_file", False)}
