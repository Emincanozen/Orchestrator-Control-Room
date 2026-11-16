"""Skills & MCP Manager: state machine over SkillState, download, activation, update, rollback.

States (domain/states.py SkillState):

    CATALOGUED_ON_DEMAND --install--> DOWNLOADED_AND_DISABLED | NEEDS_SECURITY_REVIEW | INSTALL_FAILED | INCOMPATIBLE
    INSTALL_FAILED / INCOMPATIBLE --install (retry)--> same four outcomes
    NEEDS_SECURITY_REVIEW --approved untrusted_tool_install--> DOWNLOADED_AND_DISABLED --activate--> INSTALLED_AND_ACTIVE
    DOWNLOADED_AND_DISABLED --activate--> INSTALLED_AND_ACTIVE --deactivate (last project)--> DOWNLOADED_AND_DISABLED
    update: new ref downloaded to a new directory; project copies of the old ref are removed; state returns to
            NEEDS_SECURITY_REVIEW (when the new package needs review) or DOWNLOADED_AND_DISABLED; previous_ref kept.
    rollback: previous_ref restored for the archive and for every project that had the skill active.

Every state change goes through store.change_tx: skill row + skill_events row + `skill.state_changed` event in one
transaction. Filesystem work happens before the transaction and is undone when the transaction fails.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from orchestrator.db import projects as projects_repo
from orchestrator.domain.states import SkillState as S
from orchestrator.security.approvals import ApprovalService
from orchestrator.security.paths import PathPolicyError, validate_root
from orchestrator.services.context import AppContext
from orchestrator.skills import artifacts, compat, store
from orchestrator.skills.catalog import Catalog, CatalogEntry, analyse, load_catalog
from orchestrator.skills.errors import (
    ActivationRefused, CatalogueOnly, FetchFailed, FetchRejected, InvalidSkillState, NotInstallable, SkillError,
    SkillNotFound, SourceNotAllowed,
)
from orchestrator.skills.sources import (
    FetchLimits, GithubSourcePolicy, SourcePolicy, SourceSpec, hash_tree, normalize_source, remove_tree, resolve_and_fetch,
)
from orchestrator.skills.static_inspect import inspect_package, pending_security
from orchestrator.util import now_iso

KIND_APPROVAL = "untrusted_tool_install"
DOWNLOADED_STATES = {S.DOWNLOADED_AND_DISABLED.value, S.NEEDS_SECURITY_REVIEW.value, S.INSTALLED_AND_ACTIVE.value}
INSTALLABLE_FROM = {S.CATALOGUED_ON_DEMAND.value, S.INSTALL_FAILED.value, S.INCOMPATIBLE.value}
MAX_CONTENT_CHARS = 256_000

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    S.CATALOGUED_ON_DEMAND.value: {S.DOWNLOADED_AND_DISABLED.value, S.NEEDS_SECURITY_REVIEW.value, S.INSTALL_FAILED.value, S.INCOMPATIBLE.value},
    S.INSTALL_FAILED.value: {S.DOWNLOADED_AND_DISABLED.value, S.NEEDS_SECURITY_REVIEW.value, S.INSTALL_FAILED.value, S.INCOMPATIBLE.value},
    S.INCOMPATIBLE.value: {S.DOWNLOADED_AND_DISABLED.value, S.NEEDS_SECURITY_REVIEW.value, S.INSTALL_FAILED.value, S.INCOMPATIBLE.value},
    S.NEEDS_SECURITY_REVIEW.value: {S.DOWNLOADED_AND_DISABLED.value, S.NEEDS_SECURITY_REVIEW.value},
    S.DOWNLOADED_AND_DISABLED.value: {S.INSTALLED_AND_ACTIVE.value, S.NEEDS_SECURITY_REVIEW.value, S.DOWNLOADED_AND_DISABLED.value},
    S.INSTALLED_AND_ACTIVE.value: {S.DOWNLOADED_AND_DISABLED.value, S.NEEDS_SECURITY_REVIEW.value, S.INSTALLED_AND_ACTIVE.value},
}


def _check(frm: str, to: str) -> None:
    if to != frm and to not in ALLOWED_TRANSITIONS.get(frm, set()):
        raise InvalidSkillState(f"transition {frm} -> {to} is not allowed", details={"from": frm, "to": to})


@dataclass
class ImportReport:
    total: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    not_installable: list[dict[str, Any]] = field(default_factory=list)
    warnings: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total, "created": self.created, "updated": self.updated, "unchanged": self.unchanged,
            "not_installable": self.not_installable, "warnings": self.warnings,
        }


def _ok(action: str, **extra: Any) -> dict[str, Any]:
    return {"action": action, "ok": True, **extra}


class SkillManager:
    def __init__(
        self,
        ctx: AppContext,
        approvals: ApprovalService,
        *,
        source_policy: Optional[SourcePolicy] = None,
        which: Callable[[str], Optional[str]] = shutil.which,
        platform: str = sys.platform,
        codex_agents_skills: bool = False,
        limits: FetchLimits = FetchLimits(),
    ):
        self.ctx = ctx
        self.approvals = approvals
        self.policy = source_policy or GithubSourcePolicy()
        self.which = which
        self.platform = platform
        self.codex_agents_skills = codex_agents_skills
        self.limits = limits
        self._locks: dict[str, asyncio.Lock] = {}
        self.last_import: Optional[ImportReport] = None

    def _lock(self, skill_id: str) -> asyncio.Lock:
        return self._locks.setdefault(skill_id, asyncio.Lock())

    @property
    def skills_dir(self) -> Path:
        return self.ctx.paths.skills_dir

    # ------------------------------------------------------------------ catalog import
    async def import_catalog(self, path: Optional[Path] = None) -> ImportReport:
        cat_path = Path(path) if path else self.ctx.config_dir / "skills-catalog.yaml"
        catalog = load_catalog(cat_path)
        report = ImportReport(total=len(catalog.skills))

        def fn(tx) -> None:
            for entry in catalog.skills:
                existing = store.get_tx(tx, entry.id)
                if existing is None:
                    store.insert_tx(tx, self._new_row(entry, catalog))
                    report.created += 1
                    continue
                fields = self._merge_fields(existing, entry, catalog)
                if fields:
                    fields["updated_at"] = now_iso()
                    store.update_tx(tx, entry.id, fields)
                    report.updated += 1
                else:
                    report.unchanged += 1

        await self.ctx.db.write(fn)
        rows = await self.ctx.db.read(store.list_tx)
        report.warnings = {k: v for k, v in analyse(rows).items()}
        for r in rows:
            if not r["source_url"]:
                report.not_installable.append({"id": r["id"], "reason": self.installable(r)[1]})
        self.last_import = report
        return report

    @staticmethod
    def _catalog_extras(e: CatalogEntry, catalog: Catalog) -> dict[str, Any]:
        return {
            "user_label": e.user_label, "candidates": e.candidates, "license": e.license, "maintenance": e.maintenance,
            "default_branch": e.default_branch, "requires_credentials": e.requires_credentials, "paid_service": e.paid_service,
            "priority_candidate": e.priority_candidate, "duplicates_of": e.duplicates_of, "catalog_version": catalog.catalog_version,
        }

    def _new_row(self, e: CatalogEntry, catalog: Catalog) -> dict[str, Any]:
        im = {k: v for k, v in e.install_method.items() if k not in ("catalog", "local")}
        im["catalog"] = self._catalog_extras(e, catalog)
        im["local"] = {"source": "catalog", "archives": []}
        return {
            "id": e.id, "name": e.name, "category": e.category, "kind": e.kind, "source_url": e.source_url,
            "source_verified": e.source_verified, "uncertainty": e.uncertainty, "description": e.description,
            "install_method": im, "compat": e.compat, "dependencies": e.dependencies, "permissions": e.permissions,
            "security": pending_security(e.security.risk, e.security.notes, e.permissions, e.requires_credentials),
            "state": S.CATALOGUED_ON_DEMAND.value, "pinned_ref": e.pinned_ref, "installed_ref": None, "previous_ref": None,
            "local_path": None, "project_scopes": [], "updated_at": now_iso(),
        }

    def _merge_fields(self, cur: dict[str, Any], e: CatalogEntry, catalog: Catalog) -> dict[str, Any]:
        """Catalog-owned fields are refreshed; local state (state, refs, paths, activations, user source, an
        inspected security report) is never touched, so a re-import cannot downgrade a locally changed skill."""
        new = self._new_row(e, catalog)
        local = cur["install_method"].get("local") or new["install_method"]["local"]
        im = dict(new["install_method"])
        im["local"] = local
        fields: dict[str, Any] = {}
        for col in ("name", "category", "kind", "uncertainty", "description", "compat", "dependencies", "permissions"):
            if cur[col] != new[col]:
                fields[col] = new[col]
        if im != cur["install_method"]:
            fields["install_method"] = im
        if local.get("source") != "user":
            for col in ("source_url", "source_verified"):
                if cur[col] != new[col]:
                    fields[col] = new[col]
        if cur["state"] in INSTALLABLE_FROM or cur["state"] == S.CATALOGUED_ON_DEMAND.value:
            if cur["pinned_ref"] != new["pinned_ref"] and new["pinned_ref"]:
                fields["pinned_ref"] = new["pinned_ref"]
        if not cur["security"].get("inspected") and cur["security"] != new["security"]:
            fields["security"] = new["security"]
        return fields

    # ------------------------------------------------------------------ reads
    async def get_row(self, skill_id: str) -> dict[str, Any]:
        row = await self.ctx.db.read(lambda tx: store.get_tx(tx, skill_id))
        if row is None:
            raise SkillNotFound(f"skill {skill_id} not found")
        return row

    async def all_rows(self) -> list[dict[str, Any]]:
        return await self.ctx.db.read(store.list_tx)

    def installable(self, skill: dict[str, Any]) -> tuple[bool, Optional[str]]:
        if not skill.get("source_url"):
            return False, "source URL unknown (ambiguous entry); set one with PATCH /api/skills/{id} before installing"
        try:
            self.policy.parse(skill["source_url"])
        except SourceNotAllowed as exc:
            return False, f"source not allowed: {exc.message}"
        return True, None

    def activation_info(self, skill: dict[str, Any]) -> dict[str, Any]:
        """Whether/how this skill can be activated in a project (the artefact it would write)."""
        kind = skill["kind"]
        sec = skill["security"]
        if kind == "mcp_server":
            cfg = artifacts.derive_mcp_config(skill["install_method"], skill["id"], platform=self.platform)
            if not cfg:
                return {"possible": False, "mode": "mcp_server", "reason": "no machine-usable MCP command in the catalog's documented install method; follow the documented steps manually"}
            return {"possible": True, "mode": "mcp_server", "server": cfg["name"], "entry": cfg["entry"], "file": ".mcp.json"}
        if kind in ("agent_skill", "collection"):
            if sec.get("inspected") and not sec.get("has_root_skill_md"):
                return {"possible": False, "mode": "skill_dir", "reason": "no SKILL.md at the root of the downloaded folder; choose one sub-skill by setting a /tree/<ref>/<path> source URL"}
            if kind == "collection" and not sec.get("inspected"):
                return {"possible": False, "mode": "skill_dir", "reason": "collection: choose a sub-skill (source URL with /tree/<ref>/<path>) first"}
            return {"possible": True, "mode": "skill_dir", "folder": ".claude/skills/<name>"}
        return {"possible": False, "mode": "catalogue_only", "reason": f"kind {kind} is not an installable artefact; follow the documented instructions manually"}

    # ------------------------------------------------------------------ install (download)
    async def install(self, skill_id: str) -> dict[str, Any]:
        async with self._lock(skill_id):
            skill = await self.get_row(skill_id)
            ok, why = self.installable(skill)
            if not ok:
                raise NotInstallable(why or "not installable", details={"installable": False, "reason": why})
            state = skill["state"]
            if state in DOWNLOADED_STATES:
                if skill["local_path"] and Path(skill["local_path"]).is_dir():
                    return _ok("install", changed=False, note="already downloaded", skill_id=skill_id)
                raise InvalidSkillState(f"{skill_id} is {state} but its archive is missing; use update to re-fetch")
            if state not in INSTALLABLE_FROM:
                raise InvalidSkillState(f"cannot install from {state}")

            report = compat.check_compat(skill["compat"], skill["dependencies"], which=self.which, platform=self.platform)
            if not report.ok:
                await self._fail(skill_id, S.INCOMPATIBLE, "install", report.blockers, extra={"compat_warnings": report.warnings})
                return {"action": "install", "ok": False, "state": S.INCOMPATIBLE.value, "reason": "; ".join(report.blockers), "retryable": True}

            try:
                spec = self.policy.parse(skill["source_url"])
                fetched = await asyncio.to_thread(self._fetch, skill_id, spec, skill["pinned_ref"])
            except (SourceNotAllowed, FetchFailed, FetchRejected) as exc:
                await self._fail(skill_id, S.INSTALL_FAILED, "install", [exc.message])
                return {"action": "install", "ok": False, "state": S.INSTALL_FAILED.value, "reason": exc.message, "retryable": True}
            except Exception as exc:  # unexpected: still record, never leave the skill half-installed
                await self._fail(skill_id, S.INSTALL_FAILED, "install", [f"unexpected error: {exc}"])
                return {"action": "install", "ok": False, "state": S.INSTALL_FAILED.value, "reason": str(exc), "retryable": True}

            sec = self._inspect(skill, fetched.path)
            to_state = S.NEEDS_SECURITY_REVIEW if sec["needs_review"] else S.DOWNLOADED_AND_DISABLED

            def fn(tx):
                cur = store.get_tx(tx, skill_id)
                _check(cur["state"], to_state.value)
                im = cur["install_method"]
                im["local"] = self._local_after_fetch(im["local"], fetched, cur["pinned_ref"])
                im["local"].pop("last_error", None)
                im["local"].pop("incompatible", None)
                return store.change_tx(
                    self.ctx, tx, skill_id, "download", to_state=to_state.value, ref=fetched.sha,
                    fields={"installed_ref": fetched.sha, "pinned_ref": cur["pinned_ref"] or fetched.sha, "local_path": str(fetched.path),
                            "security": sec, "install_method": im},
                    detail={"files": fetched.files, "bytes": fetched.total_bytes, "subpath": fetched.subpath, "skipped": fetched.skipped,
                            "review_reasons": sec["review_reasons"], "compat_warnings": report.warnings, "risk": sec["risk"]},
                )

            await self.ctx.db.write(fn)
            return _ok("install", changed=True, ref=fetched.sha, state=to_state.value, review_reasons=sec["review_reasons"], skill_id=skill_id)

    def _fetch(self, skill_id: str, spec: SourceSpec, ref: Optional[str]):
        return resolve_and_fetch(spec, self.skills_dir / skill_id, ref_request=ref, limits=self.limits)

    def _inspect(self, skill: dict[str, Any], path: Path) -> dict[str, Any]:
        sec = skill["security"]
        cat = skill["install_method"].get("catalog", {})
        return inspect_package(
            path,
            catalog_risk=sec.get("catalog_risk") or sec.get("risk") or "unknown",
            catalog_notes=sec.get("catalog_notes") or "",
            catalog_permissions=skill["permissions"],
            catalog_requires_credentials=bool(cat.get("requires_credentials")),
        )

    @staticmethod
    def _local_after_fetch(local: dict[str, Any], fetched, pinned: Optional[str]) -> dict[str, Any]:
        local = dict(local)
        archives = [a for a in local.get("archives", []) if a["ref"] != fetched.sha]
        archives.append({"ref": fetched.sha, "path": str(fetched.path), "subpath": fetched.subpath, "files": fetched.files, "bytes": fetched.total_bytes, "tree_sha256": fetched.tree_sha256, "fetched_at": now_iso()})
        local["archives"] = archives
        return local

    async def _fail(self, skill_id: str, state: S, action: str, reasons: list[str], extra: Optional[dict[str, Any]] = None) -> None:
        def fn(tx):
            cur = store.get_tx(tx, skill_id)
            im = cur["install_method"]
            key = "incompatible" if state == S.INCOMPATIBLE else "last_error"
            if state == S.INCOMPATIBLE:
                im["local"]["incompatible"] = reasons
            else:
                im["local"]["last_error"] = "; ".join(reasons)
            store.change_tx(self.ctx, tx, skill_id, action + "_failed", to_state=state.value, fields={"install_method": im},
                            detail={"reason": "; ".join(reasons), "retryable": True, "kind": key, **(extra or {})})

        await self.ctx.db.write(fn)

    # ------------------------------------------------------------------ activation
    async def activate(self, skill_id: str, project_id: str, *, scope: str = "project", targets: Optional[list[str]] = None) -> tuple[int, dict[str, Any]]:
        """Returns (http_status, body). 202 = approval required (nothing was activated)."""
        targets = targets or ["claude"]
        async with self._lock(skill_id):
            skill = await self.get_row(skill_id)
            if scope != "project":
                raise ActivationRefused("only project scope is supported; the orchestrator never writes global configuration")
            if "codex" in targets and not self.codex_agents_skills:
                raise ActivationRefused("Codex activation is unsupported: " + compat.CODEX_AGENTS_SKILLS_NOTE)
            if set(targets) - {"claude", "codex"}:
                raise ActivationRefused(f"unknown activation target(s): {sorted(set(targets) - {'claude', 'codex'})}")
            project = await self.ctx.db.read(lambda tx: projects_repo.get(tx, project_id))
            if project is None:
                raise SkillNotFound(f"project {project_id} not found")
            info = self.activation_info(skill)
            if not info["possible"] and skill["state"] not in DOWNLOADED_STATES:
                self._refuse_catalogue(skill, info)
            if skill["state"] not in DOWNLOADED_STATES:
                raise InvalidSkillState(f"{skill_id} is {skill['state']}; install (download) it first", details={"state": skill["state"]})
            if not info["possible"]:
                self._refuse_catalogue(skill, info)
            ref = skill["installed_ref"]
            existing = next((a for a in skill["project_scopes"] if a["project_id"] == project_id), None)
            if existing and existing["ref"] == ref:
                return 200, _ok("activate", changed=False, note="already active in this project", skill_id=skill_id)
            if existing:
                raise InvalidSkillState("an activation of another ref exists in this project; deactivate it first")

            binding = await self._binding(skill, ref, info)
            approval = await self._ensure_approval(skill, project, scope, targets, info, binding)
            if approval.status != "approved":
                return 202, {
                    "action": "activate", "ok": False, "status": "approval_required", "approval_id": approval.id,
                    "approval": approval.model_dump(mode="json"), "skill_id": skill_id, "ref": ref, "project_id": project_id,
                    "message": "An untrusted_tool_install approval is pending. Resolve it, then call activate again.",
                }
            record = await self._apply_activation(skill, project, scope, targets, info, approval.id)
            return 200, _ok("activate", changed=True, skill_id=skill_id, ref=ref, project_id=project_id, activation=record)

    def _refuse_catalogue(self, skill: dict[str, Any], info: dict[str, Any]) -> None:
        raise CatalogueOnly(
            info.get("reason") or "catalogue only",
            details={"instructions": self.instructions(skill), "mode": info.get("mode")},
        )

    # ---- approval <-> content binding (SECURITY_REVIEW #17)
    @staticmethod
    def entry_hash(entry: Any) -> str:
        return hashlib.sha256(json.dumps(entry, sort_keys=True).encode("utf-8")).hexdigest()

    async def _binding(self, skill: dict[str, Any], ref: str, info: dict[str, Any]) -> dict[str, Any]:
        """What an approval is bound to: the hash of the archive tree on disk and, for MCP servers, the exact
        entry that will be written. Refuses when the archive no longer matches the hash recorded at download."""
        src = self._archive_path(skill, ref)
        if not src.is_dir():
            raise InvalidSkillState(f"archive for ref {ref} is missing on disk")
        current = await asyncio.to_thread(hash_tree, src)
        recorded = next((a.get("tree_sha256") for a in skill["install_method"]["local"].get("archives", []) if a["ref"] == ref), None)
        if recorded and recorded != current:
            await self._invalidate_approvals(skill["id"], ref, None, "archive content changed on disk since it was downloaded")
            raise ActivationRefused(
                "the downloaded archive changed on disk since it was fetched; refusing to activate. Re-install (update) the skill.",
                details={"expected_sha256": recorded, "actual_sha256": current},
            )
        out: dict[str, Any] = {"content_sha256": current}
        if info.get("mode") == "mcp_server":
            out["mcp_entry_sha256"] = self.entry_hash(info["entry"])
        return out

    async def _invalidate_approvals(self, skill_id: str, ref: str, project_id: Optional[str], why: str) -> None:
        """Deny every pending/approved approval of (skill, ref[, project]); a fresh approval is then required."""
        from orchestrator.domain import events as ev
        from orchestrator.db import records

        def fn(tx):
            rows = tx.all(
                "SELECT id, kind, status FROM approvals WHERE kind = ? AND status IN ('pending','approved') "
                "AND json_extract(details,'$.skill_id') = ? AND json_extract(details,'$.ref') = ?",
                (KIND_APPROVAL, skill_id, ref),
            )
            for r in rows:
                records.approval_update(tx, r["id"], {"status": "denied", "resolved_at": now_iso(), "resolved_by": "system:content-binding", "note": f"invalidated: {why}"})
                self.ctx.bus.append(tx, ev.APPROVAL_RESOLVED, payload={"approval_id": r["id"], "kind": r["kind"], "approved": False,
                                                                       "by": "system:content-binding", "note": f"invalidated: {why}"})

        await self.ctx.db.write(fn)

    @staticmethod
    def _binding_matches(details: dict[str, Any], binding: dict[str, Any]) -> bool:
        return all(details.get(k) == v for k, v in binding.items()) and "content_sha256" in details

    @staticmethod
    def activation_preview(info: dict[str, Any]) -> dict[str, Any]:
        """The exact artefact an activation writes, shown in the approval."""
        if info.get("mode") == "mcp_server":
            entry = info["entry"]
            return {
                "file": ".mcp.json", "server": info["server"], "command": entry.get("command"), "args": entry.get("args", []),
                "url": entry.get("url"), "transport": entry.get("type"), "env_keys": sorted((entry.get("env") or {}).keys()),
                "entry": entry,
            }
        return {"folder": info.get("folder"), "file": info.get("file")}

    async def _ensure_approval(self, skill, project, scope, targets, info, binding):
        ref = skill["installed_ref"]

        def look(tx):
            row = store.find_approval_tx(tx, KIND_APPROVAL, skill["id"], ref, project.id)
            from orchestrator.domain.models import Approval
            return Approval.from_row(row)

        latest = await self.ctx.db.read(look)
        if latest is not None and latest.status in ("approved", "pending"):
            if self._binding_matches(latest.details, binding):
                return latest
            # approved/requested for different content (or before content binding existed): void it, ask again
            await self._invalidate_approvals(skill["id"], ref, project.id, "approval is not bound to the current content")
        sec = skill["security"]
        top = [f"{f['severity']}: {f['message']} ({f['path']})" for f in sec.get("findings", [])[:8]]
        summary = (
            f"Activate {skill['kind']} '{skill['name']}' @ {ref[:12]} in project '{project.name}' "
            f"(risk {sec.get('risk')}{'; credentials required' if sec.get('requires_credentials') else ''}). "
            f"Writes into the project only: {info.get('folder') or info.get('file')}. Nothing from the package is executed by the orchestrator."
            + (f" {self._mcp_sentence(info)}" if info.get("mode") == "mcp_server" else "")
            + f" Content sha256 {binding['content_sha256'][:16]}."
        )
        return await self.approvals.request(
            KIND_APPROVAL, summary,
            project_id=project.id,
            details={
                "skill_id": skill["id"], "skill_name": skill["name"], "ref": ref, "project_id": project.id, "scope": scope,
                "targets": targets, "source_url": skill["source_url"], "risk": sec.get("risk"), "review_reasons": sec.get("review_reasons", []),
                "findings": top, "mode": info["mode"],
                "activation": self.activation_preview(info), **binding,
            },
        )

    @staticmethod
    def _mcp_sentence(info: dict[str, Any]) -> str:
        e = info["entry"]
        what = f"url {e['url']}" if e.get("url") else f"command {e.get('command')} {' '.join(str(a) for a in e.get('args', []))}".rstrip()
        keys = ", ".join(sorted((e.get("env") or {}).keys())) or "none"
        return f"MCP server '{info['server']}' will run: {what} (env keys: {keys})."

    async def _verify_binding(self, skill, ref: str, project_id: str, info, approval_id: str) -> None:
        """Re-verify, immediately before anything is written, that the approval covers exactly this content."""
        from orchestrator.domain.models import Approval

        binding = await self._binding(skill, ref, info)

        def look(tx):
            return Approval.from_row(tx.one("SELECT * FROM approvals WHERE id = ?", (approval_id,)))

        appr = await self.ctx.db.read(look)
        if appr is None or appr.status != "approved" or not self._binding_matches(appr.details, binding):
            await self._invalidate_approvals(skill["id"], ref, project_id, "content differs from what was approved")
            raise ActivationRefused(
                "the approval does not cover the current content of this skill (it changed or predates content binding); a new approval is required",
                details={"approval_id": approval_id},
            )

    async def _apply_activation(self, skill, project, scope, targets, info, approval_id: str, *, ref: Optional[str] = None) -> dict[str, Any]:
        ref = ref or skill["installed_ref"]
        src = self._archive_path(skill, ref)
        if not src.is_dir():
            raise InvalidSkillState(f"archive for ref {ref} is missing on disk")
        await self._verify_binding(skill, ref, project.id, info, approval_id)
        try:
            root = str(validate_root(project.path))
        except PathPolicyError as exc:
            raise ActivationRefused(f"project path rejected: {exc}") from exc
        sec = skill["security"]
        artifacts_written: list[dict[str, Any]] = []
        try:
            if info["mode"] == "mcp_server":
                artifacts_written.append(await asyncio.to_thread(artifacts.write_mcp_entry, root, info["server"], info["entry"]))
                name = info["server"]
            else:
                name = artifacts.safe_name(sec.get("primary_skill_name") or skill["id"])
                for t in targets:
                    artifacts_written.append(await asyncio.to_thread(artifacts.install_skill_dir, src, root, t, name, skill["id"], ref))
            record = {"project_id": project.id, "scope": "project", "ref": ref, "name": name, "mode": info["mode"], "targets": targets,
                      "approval_id": approval_id, "activated_at": now_iso(), "artifacts": artifacts_written}

            def fn(tx):
                cur = store.get_tx(tx, skill["id"])
                scopes = [a for a in cur["project_scopes"] if a["project_id"] != project.id] + [record]
                to_state = S.INSTALLED_AND_ACTIVE.value
                if cur["state"] == S.NEEDS_SECURITY_REVIEW.value:  # an approved approval is the review
                    store.change_tx(self.ctx, tx, skill["id"], "review_cleared", to_state=S.DOWNLOADED_AND_DISABLED.value, ref=ref,
                                    detail={"approval_id": approval_id}, project_id=project.id)
                    cur = store.get_tx(tx, skill["id"])
                _check(cur["state"], to_state)
                self._profile_tx(tx, project.id, skill["id"], record, active=True)
                return store.change_tx(self.ctx, tx, skill["id"], "activate", to_state=to_state, ref=ref, fields={"project_scopes": scopes},
                                       detail={"name": name, "targets": targets, "mode": info["mode"], "approval_id": approval_id,
                                               "written": [a.get("path") for a in artifacts_written]}, project_id=project.id)

            await self.ctx.db.write(fn)
        except BaseException:
            for a in artifacts_written:
                artifacts.remove_artifact(a)
            raise
        return record

    def _archive_path(self, skill: dict[str, Any], ref: str) -> Path:
        for a in skill["install_method"]["local"].get("archives", []):
            if a["ref"] == ref:
                return Path(a["path"])
        return self.skills_dir / skill["id"] / ref

    @staticmethod
    def _profile_tx(tx, project_id: str, skill_id: str, record: Optional[dict[str, Any]], *, active: bool) -> None:
        proj = projects_repo.get(tx, project_id)
        if proj is None:
            return
        profile = dict(proj.skill_profile or {})
        act = dict(profile.get("active_skills") or {})
        if active and record:
            act[skill_id] = {"ref": record["ref"], "name": record["name"], "scope": "project", "activated_at": record["activated_at"]}
        else:
            act.pop(skill_id, None)
        profile["active_skills"] = act
        projects_repo.update(tx, project_id, {"skill_profile": profile, "updated_at": now_iso()})

    # ------------------------------------------------------------------ deactivate
    async def deactivate(self, skill_id: str, project_id: str) -> dict[str, Any]:
        async with self._lock(skill_id):
            skill = await self.get_row(skill_id)
            rec = next((a for a in skill["project_scopes"] if a["project_id"] == project_id), None)
            if rec is None:
                raise InvalidSkillState(f"{skill_id} is not active in project {project_id}")
            removal = await self._remove_activation(skill_id, rec, action="deactivate")
            return _ok("deactivate", changed=True, skill_id=skill_id, project_id=project_id, **removal)

    async def _remove_activation(self, skill_id: str, rec: dict[str, Any], *, action: str, extra_fields: Optional[dict[str, Any]] = None,
                                 to_state: Optional[str] = None, detail: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        removed: list[str] = []
        left: list[str] = []
        for art in rec.get("artifacts", []):
            res = await asyncio.to_thread(artifacts.remove_artifact, art)
            removed += res["removed"]
            left += res["left"]

        def fn(tx):
            cur = store.get_tx(tx, skill_id)
            scopes = [a for a in cur["project_scopes"] if a["project_id"] != rec["project_id"]]
            state = to_state or (S.INSTALLED_AND_ACTIVE.value if scopes else S.DOWNLOADED_AND_DISABLED.value)
            if cur["state"] == S.INSTALLED_AND_ACTIVE.value or state != cur["state"]:
                _check(cur["state"], state)
            self._profile_tx(tx, rec["project_id"], skill_id, None, active=False)
            fields = {"project_scopes": scopes, **(extra_fields or {})}
            store.change_tx(self.ctx, tx, skill_id, action, to_state=state, fields=fields,
                            detail={"removed": removed, "left": left, **(detail or {})}, project_id=rec["project_id"])

        await self.ctx.db.write(fn)
        return {"removed": removed, "left": left}

    # ------------------------------------------------------------------ update / rollback
    async def update(self, skill_id: str, ref: Optional[str] = None) -> dict[str, Any]:
        async with self._lock(skill_id):
            skill = await self.get_row(skill_id)
            if skill["state"] not in DOWNLOADED_STATES:
                raise InvalidSkillState(f"cannot update from {skill['state']}; install it first")
            ok, why = self.installable(skill)
            if not ok:
                raise NotInstallable(why or "not installable")
            spec = self.policy.parse(skill["source_url"])
            try:
                fetched = await asyncio.to_thread(self._fetch, skill_id, spec, ref)
            except (FetchFailed, FetchRejected, SourceNotAllowed) as exc:
                def note(tx):
                    cur = store.get_tx(tx, skill_id)
                    store.change_tx(self.ctx, tx, skill_id, "update_failed", detail={"reason": exc.message, "retryable": True}, ref=cur["installed_ref"])
                await self.ctx.db.write(note)
                return {"action": "update", "ok": False, "state": skill["state"], "reason": exc.message, "retryable": True}
            if fetched.sha == skill["installed_ref"]:
                return _ok("update", changed=False, note="already at the newest ref", ref=fetched.sha)
            # the old ref's project copies are removed; activating the new ref needs a new approval
            reactivate = [{"project_id": a["project_id"], "ref": a["ref"]} for a in skill["project_scopes"]]
            for rec in list(skill["project_scopes"]):
                await self._remove_activation(skill_id, rec, action="deactivate", to_state=S.DOWNLOADED_AND_DISABLED.value if len(skill["project_scopes"]) == 1 else None,
                                              detail={"reason": "update to a new ref"})
                skill = await self.get_row(skill_id)
            sec = self._inspect(skill, fetched.path)
            to_state = S.NEEDS_SECURITY_REVIEW if sec["needs_review"] else S.DOWNLOADED_AND_DISABLED
            old = skill["installed_ref"]

            def fn(tx):
                cur = store.get_tx(tx, skill_id)
                im = cur["install_method"]
                im["local"] = self._local_after_fetch(im["local"], fetched, None)
                im["local"]["pending_reactivation"] = reactivate
                _check(cur["state"], to_state.value)
                store.change_tx(
                    self.ctx, tx, skill_id, "update", to_state=to_state.value, ref=fetched.sha,
                    fields={"previous_ref": old, "installed_ref": fetched.sha, "pinned_ref": fetched.sha, "local_path": str(fetched.path),
                            "security": sec, "install_method": im},
                    detail={"from_ref": old, "to_ref": fetched.sha, "files": fetched.files, "review_reasons": sec["review_reasons"], "reactivate": reactivate},
                )

            await self.ctx.db.write(fn)
            return _ok("update", changed=True, previous_ref=old, ref=fetched.sha, state=to_state.value, reactivate=reactivate)

    async def rollback(self, skill_id: str) -> dict[str, Any]:
        async with self._lock(skill_id):
            skill = await self.get_row(skill_id)
            prev = skill["previous_ref"]
            if not prev:
                raise InvalidSkillState("nothing to roll back to (no previous_ref)")
            prev_dir = self._archive_path(skill, prev)
            if not prev_dir.is_dir():
                raise InvalidSkillState(f"archive of the previous ref {prev} is missing on disk")
            current = skill["installed_ref"]
            projects_to_restore = {a["project_id"] for a in skill["project_scopes"]}
            projects_to_restore |= {p["project_id"] for p in skill["install_method"]["local"].get("pending_reactivation", [])}
            for rec in list(skill["project_scopes"]):
                await self._remove_activation(skill_id, rec, action="deactivate", detail={"reason": "rollback"},
                                              to_state=S.DOWNLOADED_AND_DISABLED.value if len(skill["project_scopes"]) == 1 else None)
                skill = await self.get_row(skill_id)
            sec = inspect_package(prev_dir, catalog_risk=skill["security"].get("catalog_risk", "unknown"), catalog_notes=skill["security"].get("catalog_notes", ""),
                                  catalog_permissions=skill["permissions"], catalog_requires_credentials=bool(skill["install_method"]["catalog"].get("requires_credentials")))

            def fn(tx):
                cur = store.get_tx(tx, skill_id)
                reviewed = store.has_approved_any_project_tx(tx, KIND_APPROVAL, skill_id, prev)
                to_state = S.NEEDS_SECURITY_REVIEW if (sec["needs_review"] and not reviewed) else S.DOWNLOADED_AND_DISABLED
                _check(cur["state"], to_state.value)
                im = cur["install_method"]
                im["local"].pop("pending_reactivation", None)
                store.change_tx(self.ctx, tx, skill_id, "rollback", to_state=to_state.value, ref=prev,
                                fields={"installed_ref": prev, "pinned_ref": prev, "previous_ref": None, "local_path": str(prev_dir), "security": sec, "install_method": im},
                                detail={"from_ref": current, "to_ref": prev})

            await self.ctx.db.write(fn)
            restored: list[str] = []
            skipped: list[dict[str, str]] = []
            for pid in sorted(projects_to_restore):
                project = await self.ctx.db.read(lambda tx, pid=pid: projects_repo.get(tx, pid))
                if project is None:
                    skipped.append({"project_id": pid, "reason": "project no longer exists"})
                    continue
                appr = await self.ctx.db.read(lambda tx, pid=pid: store.find_approval_tx(tx, KIND_APPROVAL, skill_id, prev, pid, "approved"))
                if appr is None:
                    skipped.append({"project_id": pid, "reason": f"no approved approval for ref {prev[:12]} in this project"})
                    continue
                skill = await self.get_row(skill_id)
                info = self.activation_info(skill)
                try:
                    await self._apply_activation(skill, project, "project", ["claude"], info, appr["id"], ref=prev)
                    restored.append(pid)
                except SkillError as exc:
                    skipped.append({"project_id": pid, "reason": exc.message})
            return _ok("rollback", changed=True, ref=prev, rolled_back_from=current, restored_projects=restored, skipped=skipped)

    # ------------------------------------------------------------------ content / PATCH
    async def content(self, skill_id: str) -> dict[str, Any]:
        skill = await self.get_row(skill_id)
        if skill["state"] not in DOWNLOADED_STATES or not skill["local_path"]:
            raise InvalidSkillState("SKILL.md is only available once the skill is downloaded", details={"state": skill["state"]})
        root = Path(skill["local_path"])
        for p in sorted(root.iterdir()) if root.is_dir() else []:
            if p.is_file() and p.name.lower() == "skill.md":
                text = p.read_bytes()[: MAX_CONTENT_CHARS * 2].decode("utf-8", errors="replace")
                return {"skill_id": skill_id, "ref": skill["installed_ref"], "path": p.name, "text": text[:MAX_CONTENT_CHARS], "truncated": len(text) > MAX_CONTENT_CHARS}
        raise SkillNotFound("this package has no SKILL.md at its root")

    async def set_source(self, skill_id: str, url: str) -> dict[str, Any]:
        async with self._lock(skill_id):
            skill = await self.get_row(skill_id)
            if skill["state"] not in INSTALLABLE_FROM:
                raise InvalidSkillState(f"cannot change the source of a {skill['state']} skill")
            self.policy.parse(url)  # raises SourceNotAllowed (422)

            def fn(tx):
                cur = store.get_tx(tx, skill_id)
                im = cur["install_method"]
                im["local"]["source"] = "user"
                im["local"].setdefault("original_source_url", cur["source_url"])
                store.change_tx(self.ctx, tx, skill_id, "set_source", fields={"source_url": url, "source_verified": False, "install_method": im},
                                detail={"source": "user", "source_url": url, "previous": cur["source_url"]})

            await self.ctx.db.write(fn)
            return _ok("set_source", changed=True, skill_id=skill_id)

    # ------------------------------------------------------------------ helpers for views
    def instructions(self, skill: dict[str, Any]) -> dict[str, Any]:
        im = skill["install_method"]
        info = self.activation_info(skill)
        steps = []
        if skill.get("source_url"):
            steps.append("POST /api/skills/{id}/install downloads the pinned commit (files only, nothing is executed).")
            steps.append("POST /api/skills/{id}/activate {project_id, scope:'project'} requests an untrusted_tool_install approval, then writes into the project.")
        else:
            steps.append("No source URL is known; set one with PATCH /api/skills/{id} {source_url}.")
        return {
            "documented_method": {"type": im.get("type"), "command": im.get("command"), "notes": im.get("notes")},
            "activation": info,
            "orchestrator_steps": steps,
            "note": "Documented commands (npx/skills CLI/plugin marketplace) are NOT run by the orchestrator.",
        }

    def warnings_for(self, rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        return analyse(rows)
