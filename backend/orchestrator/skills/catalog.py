"""Catalog loading + validation (config/skills-catalog.yaml) and duplicate / conflict analysis."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orchestrator.skills.errors import CatalogError
from orchestrator.skills.sources import normalize_source

Kind = Literal["agent_skill", "mcp_server", "standalone_app", "dev_tool", "collection", "external_service", "docs"]
Risk = Literal["low", "medium", "high", "unknown"]


class CatalogSecurity(BaseModel):
    model_config = ConfigDict(extra="allow")
    risk: Risk = "unknown"
    notes: str = ""


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    user_label: Optional[str] = None
    name: str = Field(min_length=1)
    kind: Kind
    category: Optional[str] = None
    source_url: Optional[str] = None
    source_verified: bool = False
    uncertainty: Optional[str] = None
    candidates: list[str] = Field(default_factory=list)
    description: str = ""
    license: Optional[str] = None
    maintenance: dict[str, Any] = Field(default_factory=dict)
    default_branch: Optional[str] = None
    pinned_ref: Optional[str] = None
    compat: dict[str, Any] = Field(default_factory=dict)
    install_method: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    requires_credentials: bool = False
    paid_service: Optional[str] = None
    security: CatalogSecurity = Field(default_factory=CatalogSecurity)
    priority_candidate: bool = False
    initial_state: Literal["CATALOGUED_ON_DEMAND"] = "CATALOGUED_ON_DEMAND"
    duplicates_of: Optional[str] = None


class Catalog(BaseModel):
    catalog_version: int = 1
    researched_at: Optional[str] = None
    skills: list[CatalogEntry]


def load_catalog(path: Path) -> Catalog:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogError(f"cannot read catalog {path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("skills"), list):
        raise CatalogError("catalog must be a mapping with a 'skills' list")
    problems: list[Any] = []
    entries: list[CatalogEntry] = []
    for i, item in enumerate(raw["skills"]):
        try:
            entries.append(CatalogEntry.model_validate(item))
        except ValidationError as exc:
            ident = item.get("id") if isinstance(item, dict) else None
            problems.append({"index": i, "id": ident, "errors": [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]})
    seen: set[str] = set()
    for e in entries:
        if e.id in seen:
            problems.append({"id": e.id, "errors": ["duplicate id"]})
        seen.add(e.id)
    for e in entries:
        if e.duplicates_of and e.duplicates_of not in seen:
            problems.append({"id": e.id, "errors": [f"duplicates_of references unknown id {e.duplicates_of!r}"]})
    if problems:
        raise CatalogError(f"invalid catalog ({len(problems)} problem(s))", problems)
    return Catalog(catalog_version=int(raw.get("catalog_version") or 1), researched_at=str(raw.get("researched_at") or ""), skills=entries)


# ---- duplicate / conflict analysis -----------------------------------------


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", text.strip().lower()).strip("-.") or "skill"


def predicted_name(skill: dict[str, Any]) -> str:
    """The folder / MCP server name this skill would use in a project."""
    report = (skill.get("security") or {})
    declared = (report.get("primary_skill_name") or "") if report.get("inspected") else ""
    if declared:
        return slug(declared)
    src = skill.get("source_url") or ""
    m = re.search(r"/tree/[^/]+/(.+?)/?$", src)
    if m:
        return slug(m.group(1).split("/")[-1])
    m = re.match(r"^https://github\.com/[^/]+/([^/]+?)(?:\.git)?/?$", src)
    return slug(m.group(1)) if m else slug(skill["id"])


def analyse(skills: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Per skill id: warnings of type `duplicate` (same normalised source URL, or declared duplicates_of) and
    `conflict` (two skills of an installable kind that would install the same skill / MCP server name)."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_url: dict[str, list[str]] = defaultdict(list)
    for s in skills:
        key = normalize_source(s.get("source_url"))
        if key:
            by_url[key].append(s["id"])
    for key, ids in by_url.items():
        if len(ids) > 1:
            for i in ids:
                out[i].append({"type": "duplicate", "reason": "same_source_url", "with": [x for x in ids if x != i], "source": key})
    for s in skills:
        dup = ((s.get("install_method") or {}).get("catalog") or {}).get("duplicates_of")
        if dup:
            out[s["id"]].append({"type": "duplicate", "reason": "duplicates_of", "with": [dup]})
            if not any(w.get("reason") == "duplicates_of" and s["id"] in w["with"] for w in out[dup]):
                out[dup].append({"type": "duplicate", "reason": "duplicated_by", "with": [s["id"]]})
    by_name: dict[tuple[str, str], list[str]] = defaultdict(list)
    for s in skills:
        if s["kind"] in ("agent_skill", "mcp_server") and s.get("source_url"):
            by_name[("mcp" if s["kind"] == "mcp_server" else "skill", predicted_name(s))].append(s["id"])
    for (family, name), ids in by_name.items():
        if len(ids) > 1:
            for i in ids:
                out[i].append({
                    "type": "conflict", "scope": "project", "name": name,
                    "reason": f"would install the same {'MCP server' if family == 'mcp' else 'skill'} name into the same scope",
                    "with": [x for x in ids if x != i],
                })
    return out
