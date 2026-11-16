"""Human approval gates (ARCHITECTURE §12).

request: creates a pending approval (or auto-approves when the kind is listed
in policies.approvals.auto_approve) and emits approval.requested.
resolve: approved -> linked task back to QUEUED (or COMPLETED for
`unverifiable_completion`, recorded as verification `unverifiable_accepted`);
denied -> linked task BLOCKED with the reason.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from orchestrator.db import projects as projects_repo
from orchestrator.db import records
from orchestrator.db import tasks as tasks_repo
from orchestrator.db.database import Tx
from orchestrator.domain import events as ev
from orchestrator.domain.models import Approval
from orchestrator.domain.states import TaskState
from orchestrator.errors import Conflict, NotFound
from orchestrator.gitops import git
from orchestrator.services.context import AppContext
from orchestrator.services.tasks import transition_tx
from orchestrator.util import new_id, now_iso

log = logging.getLogger(__name__)
S = TaskState

KIND_UNVERIFIABLE = "unverifiable_completion"
KIND_GIT_INIT = "git_init_in_project"
KIND_PERMISSION = "permission_escalation"
KIND_HIGH_RISK = "high_risk_task"  # router classified the task as critical risk (routing.yaml risk.critical)
# M3 (leader): approving adopts the proposed verify commands; clarification/escalation approvals just re-queue.
KIND_LEADER_VERIFY = "leader_verify_commands"
KIND_LEADER_QUESTIONS = "leader_clarification"
KIND_LEADER_ESCALATION = "leader_escalation"
KIND_LEADER_BUDGET = "leader_budget"

# State reason prefix of a task blocked for a security finding (verification sandbox unavailable or tampered,
# unsafe worktree content). Such a task can only be completed by a human (N2 re-review).
SECURITY_REASON_PREFIX = "security:"
# Kinds that are never auto-approved for a task with a security finding in its history.
_HUMAN_ONLY_AFTER_SECURITY = frozenset({KIND_UNVERIFIABLE})


def has_security_finding_tx(tx: Tx, task_id: str) -> bool:
    """True when the task was ever blocked for a security reason or a verification of it ended in a security
    error (`sandbox_unavailable:` / `sandbox_tamper:`)."""
    from orchestrator.security.sandbox import is_security_error

    if tx.one("SELECT 1 FROM state_transitions WHERE task_id = ? AND reason LIKE ?", (task_id, SECURITY_REASON_PREFIX + "%")):
        return True
    return any(is_security_error(c.error) for v in records.verifications_for_task(tx, task_id) for c in v.checks)


def apply_leader_approval_tx(tx: Tx, task_id: str, approval: Approval) -> None:
    """Side effects of an approved leader gate. Only `leader_verify_commands` changes data: the commands the
    safety filter refused become the task's verify commands because a human approved exactly that list."""
    if approval.kind != KIND_LEADER_VERIFY:
        return
    commands = approval.details.get("verify_commands")
    task = tasks_repo.get(tx, task_id)
    if task is not None and not task.verify_commands and isinstance(commands, list) and commands:
        plan = dict(task.plan) if isinstance(task.plan, dict) else {}
        plan["verify_origin"] = "approved"  # a human approved exactly this list
        tasks_repo.update(tx, task_id, {"verify_commands": commands, "plan": plan, "updated_at": now_iso()})

# What approving a permission_escalation can really change (ExecutionRequest.permission has only two
# profiles, so this is all the orchestrator can grant):
#   read_only run denied (plan/review/advise) -> the next run of THAT phase for the task gets `workspace_write`.
#   workspace_write run denied (execute/repair) -> NOTHING: that is already the highest profile.
# What `workspace_write` means differs per provider; `workspace_write_meaning` states it without overclaiming.
GRANT_WORKSPACE_WRITE = "workspace_write"


def workspace_write_meaning(provider: str, *, claude_shell: bool = False) -> str:
    """One honest sentence on what a workspace_write run of `provider` may do (security findings #3/#4)."""
    if provider == "claude":
        if claude_shell:
            return ("For Claude this means the file tools (confined to the task worktree by --restricted) PLUS a "
                    "Bash shell (worker_permissions.claude_shell is on): shell commands are NOT confined to the "
                    "worktree and can reach the network and any file this Windows user can access.")
        return ("For Claude this means file tools only (Read/Write/Edit/Glob/Grep), confined to the task worktree "
                "by --restricted; no shell and no web tools.")
    if provider == "codex":
        # Verified with real runs on 2026-11-12 (codex exec -s workspace-write on Windows): writes outside the
        # workspace were refused except under %TEMP%; a file outside the workspace was read; the shell ran.
        return ("For Codex this means its own workspace-write sandbox with a shell: Codex limits WRITES to the task "
                "worktree and the temp directory, but the worker can READ any file this Windows user can read and "
                "run shell commands.")
    if provider == "antigravity":
        return ("For Antigravity this means its own accept-edits mode; shell commands are auto-denied in headless "
                "runs. Which paths it may read is decided by agy, not by the orchestrator.")
    return f"For {provider} the meaning of workspace_write is defined by that provider's own permission model."


def permission_escalation_text(
    task_title: str, provider: str, phase: str, used: str, reason: str, *, claude_shell: bool = False
) -> tuple[str, Optional[str]]:
    """(approval summary, grant) for a permission_escalation request. grant is None when nothing can be granted."""
    head = f"Task '{task_title}': the {phase} run via {provider} was denied a permission (it ran as {used}): {reason}. "
    meaning = workspace_write_meaning(provider, claude_shell=claude_shell)
    if used == "read_only":
        return (
            head
            + f"Approving lets the next {phase} run of this task use workspace_write. {meaning} It never adds "
            "dangerous-skip flags. If the denied action needs more than that, the run will be denied again. "
            "Denying blocks the task.",
            GRANT_WORKSPACE_WRITE,
        )
    return (
        head
        + f"This run already had workspace_write, the highest permission the orchestrator can give. {meaning} "
        "Approving only re-queues the task: it grants NO additional permission, so the same action will likely be "
        "denied again. Further access cannot be granted automatically; deny to block the task and perform that "
        "step manually.",
        None,
    )


def request_tx(
    ctx: AppContext,
    tx: Tx,
    kind: str,
    summary: str,
    *,
    details: Optional[dict[str, Any]] = None,
    task_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Approval:
    # One pending approval per (task, kind): repeated requests return the open one.
    if task_id:
        for a in records.approval_list(tx, status="pending", task_id=task_id):
            if a.kind == kind:
                return a
    policies = ctx.policies_tx(tx)
    auto = kind in policies.auto_approve
    if auto and task_id and kind in _HUMAN_ONLY_AFTER_SECURITY and has_security_finding_tx(tx, task_id):
        auto = False  # a security finding is never accepted by policy: a human decides
    ts = now_iso()
    approval_id = new_id("apr")
    det = dict(details or {})
    if project_id:
        det.setdefault("project_id", project_id)
    records.approval_insert(
        tx,
        {
            "id": approval_id,
            "task_id": task_id,
            "kind": kind,
            "summary": summary,
            "details": det,
            "status": "approved" if auto else "pending",
            "requested_at": ts,
            "resolved_at": ts if auto else None,
            "resolved_by": "policy:auto_approve" if auto else None,
        },
    )
    approval = records.approval_get(tx, approval_id)
    assert approval is not None
    ctx.bus.append(
        tx,
        ev.APPROVAL_REQUESTED,
        project_id=project_id,
        task_id=task_id,
        payload={"approval": approval.model_dump(mode="json"), "auto_approved": auto},
    )
    if auto:
        ctx.bus.append(
            tx,
            ev.APPROVAL_RESOLVED,
            project_id=project_id,
            task_id=task_id,
            payload={"approval_id": approval_id, "kind": kind, "approved": True, "by": "policy:auto_approve"},
        )
    return approval


def _record_unverifiable_accepted(tx: Tx, task_id: str, approval_id: str) -> None:
    ts = now_iso()
    records.verification_insert(
        tx,
        {
            "id": new_id("vrf"),
            "task_id": task_id,
            "status": "unverifiable_accepted",
            "checks": [],
            "started_at": ts,
            "finished_at": ts,
        },
    )


def resolve_tx(
    ctx: AppContext, tx: Tx, approval_id: str, approve: bool, *, note: Optional[str], by: str
) -> Approval:
    approval = records.approval_get(tx, approval_id)
    if approval is None:
        raise NotFound(f"approval {approval_id} not found")
    if approval.status != "pending":
        raise Conflict(f"approval {approval_id} is already {approval.status}")
    ts = now_iso()
    records.approval_update(
        tx, approval_id, {"status": "approved" if approve else "denied", "resolved_at": ts, "resolved_by": by, "note": note}
    )
    project_id = approval.details.get("project_id")
    task = tasks_repo.get(tx, approval.task_id) if approval.task_id else None
    if task is not None:
        project_id = task.project_id
    ctx.bus.append(
        tx,
        ev.APPROVAL_RESOLVED,
        project_id=project_id,
        task_id=approval.task_id,
        payload={"approval_id": approval_id, "kind": approval.kind, "approved": approve, "by": by, "note": note},
    )
    if task is not None and approve and approval.kind == KIND_PERMISSION and approval.details.get("grants") == GRANT_WORKSPACE_WRITE:
        tasks_repo.update(
            tx, task.id,
            {"granted_permission": {"permission": GRANT_WORKSPACE_WRITE, "phase": approval.details.get("phase"), "approval_id": approval_id}},
        )
    if task is not None and approve:
        apply_leader_approval_tx(tx, task.id, approval)
    if task is not None and task.state == S.NEEDS_APPROVAL:
        if approve and approval.kind == KIND_UNVERIFIABLE:
            _record_unverifiable_accepted(tx, task.id, approval_id)
            transition_tx(ctx, tx, task.id, S.COMPLETED, reason=f"approved: {approval.kind}", actor=by)
        elif approve:
            transition_tx(ctx, tx, task.id, S.QUEUED, reason=f"approved: {approval.kind}", actor=by)
        else:
            reason = f"denied: {approval.kind}" + (f": {note}" if note else "")
            transition_tx(ctx, tx, task.id, S.BLOCKED, reason=reason, actor=by)
    out = records.approval_get(tx, approval_id)
    assert out is not None
    return out


class ApprovalService:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx

    async def list(self, status: Optional[str] = None, task_id: Optional[str] = None) -> list[Approval]:
        return await self.ctx.db.read(lambda tx: records.approval_list(tx, status, task_id))

    async def request(self, kind: str, summary: str, **kwargs: Any) -> Approval:
        return await self.ctx.db.write(lambda tx: request_tx(self.ctx, tx, kind, summary, **kwargs))

    async def resolve(self, approval_id: str, approve: bool, *, note: Optional[str] = None, by: str = "user") -> Approval:
        approval = await self.ctx.db.write(lambda tx: resolve_tx(self.ctx, tx, approval_id, approve, note=note, by=by))
        if approve and approval.kind == KIND_GIT_INIT:
            await self.apply_git_init(approval)
        return approval

    async def apply_git_init(self, approval: Approval) -> None:
        project_id = approval.details.get("project_id")
        project = await self.ctx.db.read(lambda tx: projects_repo.get(tx, project_id)) if project_id else None
        if project is None or project.is_git:
            return
        try:
            await git.agit(git.init_repo, project.path)
            branch = await git.agit(git.current_branch, project.path)
        except git.GitError as exc:
            await self.ctx.bus.emit(
                ev.SYSTEM_NOTICE, project_id=project.id, payload={"level": "error", "message": f"git init failed: {exc}"}
            )
            return

        def fn(tx: Tx) -> None:
            projects_repo.update(
                tx,
                project.id,
                {"is_git": True, "isolation": "worktree", "default_branch": branch, "updated_at": now_iso()},
            )
            ctx_bus = self.ctx.bus
            ctx_bus.append(
                tx,
                ev.SYSTEM_NOTICE,
                project_id=project.id,
                payload={"level": "info", "message": "project initialised as git repository; worktree isolation on"},
            )

        await self.ctx.db.write(fn)
