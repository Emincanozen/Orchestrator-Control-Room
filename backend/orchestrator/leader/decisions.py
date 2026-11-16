"""Decision records (ADR-style rows in `decisions`) written by the advisor flow and the leader.

Evidence rule (ARCHITECTURE section 10): a decision must cite OBJECTIVE evidence — a verification id, a
commit ref or a diff ref. Run ids alone are provenance, not evidence, and "the models agree" is not a
rationale. A decision without objective evidence is still stored, with `evidence_status: "none"` and a
`no_objective_evidence` flag, so it can be found and challenged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Optional

from orchestrator.db.database import Tx
from orchestrator.db import projects as projects_repo
from orchestrator.db import repo
from orchestrator.domain import events as ev
from orchestrator.domain.models import Decision
from orchestrator.errors import Conflict, NotFound, ValidationFailed
from orchestrator.security.paths import PathPolicyError, resolve_inside
from orchestrator.services.context import AppContext
from orchestrator.util import new_id, now_iso, truncate

OBJECTIVE_REF_TYPES = frozenset({"verification", "commit", "diff"})
MAX_LIST = 200


def evidence_status(evidence: Iterable[dict[str, Any]]) -> str:
    return "verified" if any(e.get("type") in OBJECTIVE_REF_TYPES for e in evidence if isinstance(e, dict)) else "none"


def record_tx(
    ctx: AppContext,
    tx: Tx,
    *,
    kind: str,
    title: str,
    context: str,
    options: list[Any],
    decision: str,
    rationale: str,
    evidence: list[dict[str, Any]],
    project_id: Optional[str] = None,
    task_id: Optional[str] = None,
    advisor_run_id: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> Decision:
    status = evidence_status(evidence)
    meta = dict(meta or {})
    if status == "none":
        meta.setdefault("flags", [])
        if "no_objective_evidence" not in meta["flags"]:
            meta["flags"].append("no_objective_evidence")
    did = new_id("dec")
    repo.insert(
        tx,
        "decisions",
        {
            "id": did, "project_id": project_id, "task_id": task_id, "title": truncate(title, 300, keep="head"),
            "context": truncate(context, 8000, keep="head"), "options": options,
            "decision": truncate(decision, 4000, keep="head"), "rationale": truncate(rationale, 8000, keep="head"),
            "evidence": evidence, "advisor_run_id": advisor_run_id, "kind": kind, "evidence_status": status,
            "meta": meta, "created_at": now_iso(),
        },
        ("options", "evidence", "meta"),
    )
    row = Decision.from_row(tx.one("SELECT * FROM decisions WHERE id = ?", (did,)))
    assert row is not None
    ctx.bus.append(
        tx, ev.DECISION_RECORDED, project_id=project_id, task_id=task_id, run_id=advisor_run_id,
        payload={
            "decision_id": did, "kind": kind, "title": row.title, "decision": row.decision,
            "evidence_status": status, "evidence": evidence, "advisor_run_id": advisor_run_id, "flags": meta.get("flags", []),
        },
    )
    return row


class DecisionService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    async def list(self, *, project_id: Optional[str] = None, task_id: Optional[str] = None, limit: int = 100) -> list[Decision]:
        def fn(tx: Tx) -> list[Decision]:
            sql, params = "SELECT * FROM decisions WHERE 1=1", []
            if project_id:
                sql += " AND project_id = ?"
                params.append(project_id)
            if task_id:
                sql += " AND task_id = ?"
                params.append(task_id)
            sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
            params.append(max(1, min(int(limit), MAX_LIST)))
            return [Decision.from_row(r) for r in tx.all(sql, params)]  # type: ignore[misc]

        return await self.ctx.db.read(fn)

    async def get(self, decision_id: str) -> Decision:
        d = await self.ctx.db.read(lambda tx: Decision.from_row(tx.one("SELECT * FROM decisions WHERE id = ?", (decision_id,))))
        if d is None:
            raise NotFound(f"decision {decision_id} not found")
        return d

    # ------------------------------------------------------------------ explicit export to docs/DECISIONS.md
    @staticmethod
    def render_markdown(d: Decision) -> str:
        lines = [
            f"## Decision {d.id}: {d.title}",
            f"<!-- decision:{d.id} -->",
            f"**Date.** {d.created_at}  ",
            f"**Kind.** {d.kind}  ",
            "",
            f"**Context.** {d.context or '-'}",
            "",
            "**Options considered.**",
        ]
        lines += [f"- {o if isinstance(o, str) else o}" for o in d.options] or ["- (none recorded)"]
        lines += ["", f"**Decision.** {d.decision or '-'}", "", f"**Rationale.** {d.rationale or '-'}", "", "**Evidence.**"]
        for e in d.evidence:
            if isinstance(e, dict):
                lines.append(f"- {e.get('type', 'ref')}: {e.get('id') or e.get('ref') or ''}")
        if not d.evidence:
            lines.append("- (none)")
        lines.append(f"- evidence status: {d.evidence_status}" + (" (flagged: no objective evidence)" if d.evidence_status == "none" else ""))
        if d.advisor_run_id:
            ind = d.meta.get("independence")
            lines += ["", f"**Advisor run.** {d.advisor_run_id}" + (f" (independence: {ind})" if ind else "")]
        return "\n".join(lines) + "\n"

    async def export(self, decision_id: str) -> dict[str, Any]:
        """Append the decision to <project path>/docs/DECISIONS.md. Only ever called on explicit request."""
        d = await self.get(decision_id)
        project_id = d.project_id
        if project_id is None and d.task_id:
            row = await self.ctx.db.one("SELECT project_id FROM tasks WHERE id = ?", (d.task_id,))
            project_id = row["project_id"] if row else None
        if project_id is None:
            raise ValidationFailed("decision is not linked to a project; nothing to export to")
        project = await self.ctx.db.read(lambda tx: projects_repo.get(tx, project_id))
        if project is None:
            raise NotFound(f"project {project_id} not found")
        root = Path(os.path.realpath(project.path))
        try:
            target = resolve_inside(os.path.join("docs", "DECISIONS.md"), [root], base=str(root))
        except PathPolicyError as exc:
            raise ValidationFailed(f"export path rejected: {exc}") from exc
        marker = f"<!-- decision:{d.id} -->"
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        if marker in existing:
            raise Conflict(f"decision {d.id} is already in {target}", code="already_exported")
        target.parent.mkdir(parents=True, exist_ok=True)
        entry = self.render_markdown(d)
        with target.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(("\n" if existing and not existing.endswith("\n\n") else "") + entry + "\n")
        return {"decision_id": d.id, "path": str(target), "appended": True}
