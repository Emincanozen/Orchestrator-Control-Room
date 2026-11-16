"""Pydantic models for persisted entities and API DTOs (ARCHITECTURE §3, §6).

Entity models mirror table rows; `from_row` decodes JSON columns. DTOs are the
request/response shapes of the HTTP API.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, ClassVar, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestrator.domain.states import AgentActivity, RunPhase, RunStatus, TaskState


class RowModel(BaseModel):
    model_config = ConfigDict(use_enum_values=False, extra="ignore")
    __json__: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def from_row(cls, row: sqlite3.Row | dict[str, Any] | None):
        if row is None:
            return None
        data = dict(row)
        for key in cls.__json__:
            raw = data.get(key)
            if isinstance(raw, (str, bytes)):
                data[key] = json.loads(raw) if raw else None
        return cls.model_validate(data)


# --------------------------------------------------------------------------- #
# Verification specs
# --------------------------------------------------------------------------- #


class VerifyCheck(BaseModel):
    """Structured verification entry. Exactly one of cmd / file_exists / file_absent / file_unchanged / json_valid."""

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    cmd: Optional[Union[list[str], str]] = None
    timeout: Optional[float] = Field(default=None, gt=0, le=24 * 3600)
    file_exists: Optional[str] = None
    contains: Optional[str] = None  # with file_exists: file must contain this text
    file_absent: Optional[str] = None  # path (relative to the worktree) that must NOT exist
    file_unchanged: Optional[str] = None  # path whose content must equal the task's base commit (git, no shell)
    json_valid: Optional[str] = None  # path of a file that must parse as JSON


VerifyCommand = Union[list[str], str, VerifyCheck]


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #

Isolation = Literal["worktree", "in_place"]


class Project(RowModel):
    __json__ = ("verify_commands", "skill_profile")
    id: str
    name: str
    path: str
    default_branch: Optional[str] = None
    verify_commands: list[VerifyCommand] = Field(default_factory=list)
    skill_profile: dict[str, Any] = Field(default_factory=dict)
    is_git: bool = False
    isolation: Isolation = "worktree"
    created_at: str
    updated_at: str


class Objective(RowModel):
    __json__ = ("deliverables", "acceptance_criteria", "prerequisites", "plan")
    id: str
    project_id: str
    text: str
    deliverables: list[Any] = Field(default_factory=list)
    acceptance_criteria: list[Any] = Field(default_factory=list)
    prerequisites: list[Any] = Field(default_factory=list)
    status: str = "draft"
    plan: Optional[Any] = None
    created_at: str
    updated_at: str


class Task(RowModel):
    __json__ = (
        "acceptance_criteria",
        "verify_commands",
        "plan",
        "completed_steps",
        "pending_steps",
        "relevant_files",
        "route_exclusions",
        "granted_permission",
    )
    id: str
    project_id: str
    objective_id: Optional[str] = None
    title: str
    description: str = ""
    task_type: str = "coding"
    priority: int = 50
    state: TaskState
    state_reason: Optional[str] = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    verify_commands: list[VerifyCommand] = Field(default_factory=list)
    plan: Optional[Any] = None
    completed_steps: list[Any] = Field(default_factory=list)
    pending_steps: list[Any] = Field(default_factory=list)
    next_action: Optional[str] = None
    relevant_files: list[str] = Field(default_factory=list)
    role: Optional[str] = None
    preferred_provider: Optional[str] = None
    preferred_model: Optional[str] = None
    attempt_count: int = 0
    consecutive_failures: int = 0
    retry_count: int = 0
    repair_cycles: int = 0
    not_before: Optional[str] = None
    worktree_path: Optional[str] = None
    branch: Optional[str] = None
    base_commit: Optional[str] = None
    head_commit: Optional[str] = None
    active_seconds: float = 0.0
    last_activity_at: Optional[str] = None
    created_at: str
    updated_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    depends_on: list[str] = Field(default_factory=list)  # filled by the repository
    route_exclusions: list[dict[str, Any]] = Field(default_factory=list)  # fallback exclusions (M2b)
    provider_switches: int = 0
    granted_permission: Optional[dict[str, Any]] = None  # approved permission_escalation (M2b)


class Run(RowModel):
    __json__ = ("usage",)
    id: str
    task_id: Optional[str] = None  # None: a leader/advisor call that belongs to no task (decomposition, switch validation)
    phase: RunPhase
    agent_id: Optional[str] = None
    provider: str
    model: Optional[str] = None
    session_id: Optional[str] = None
    pid: Optional[int] = None
    status: RunStatus
    error_category: Optional[str] = None
    error_message: Optional[str] = None
    exit_code: Optional[int] = None
    usage: dict[str, Any] = Field(default_factory=dict)
    started_at: str
    finished_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    idempotency_key: str


class StateTransition(RowModel):
    id: int
    task_id: str
    from_state: Optional[str] = None
    to_state: str
    reason: Optional[str] = None
    actor: str
    ts: str


CheckpointKind = Literal["auto", "handoff", "leadership", "manual"]


class Checkpoint(RowModel):
    __json__ = ("summary",)
    id: str
    task_id: Optional[str] = None  # None for `leadership` checkpoints (they span all tasks)
    run_id: Optional[str] = None
    kind: CheckpointKind
    summary: dict[str, Any]
    git_ref: Optional[str] = None
    created_at: str


VerificationStatus = Literal["passed", "failed", "error", "no_checks", "unverifiable_accepted"]


class CheckResult(BaseModel):
    name: str
    kind: Literal["command", "file"]
    argv: Optional[list[str]] = None
    path: Optional[str] = None
    passed: bool
    exit_code: Optional[int] = None
    timed_out: bool = False
    duration_seconds: float = 0.0
    output: str = ""  # truncated + redacted
    error: Optional[str] = None


class Verification(RowModel):
    __json__ = ("checks",)
    id: str
    task_id: str
    run_id: Optional[str] = None
    status: VerificationStatus
    checks: list[CheckResult] = Field(default_factory=list)
    origin: Optional[Literal["user", "leader", "approved"]] = None  # who designed the checks (None: older records)
    started_at: str
    finished_at: Optional[str] = None


class Agent(RowModel):
    id: str
    name: str
    role: str
    room: str
    provider: Optional[str] = None
    model: Optional[str] = None
    activity: AgentActivity = AgentActivity.IDLE
    current_task_id: Optional[str] = None
    current_run_id: Optional[str] = None
    current_operation: Optional[str] = None
    since: str
    seat_order: int = 0


ApprovalStatus = Literal["pending", "approved", "denied"]


class Approval(RowModel):
    __json__ = ("details",)
    id: str
    task_id: Optional[str] = None
    kind: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    status: ApprovalStatus = "pending"
    requested_at: str
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    note: Optional[str] = None


class Decision(RowModel):
    __json__ = ("options", "evidence", "meta")
    id: str
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    title: str
    context: str = ""
    options: list[Any] = Field(default_factory=list)
    decision: str = ""
    rationale: str = ""
    evidence: list[Any] = Field(default_factory=list)
    advisor_run_id: Optional[str] = None
    kind: str = "leader_decision"  # advisor_consult | leader_decision
    evidence_status: str = "none"  # verified | none ("none" = no objective evidence; flagged)
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class RoutingDecision(RowModel):
    __json__ = ("candidates", "reasons", "rule_ids", "detail")
    id: str
    task_id: str
    run_id: Optional[str] = None
    phase: str
    chosen_provider: str
    chosen_model: Optional[str] = None
    candidates: list[Any] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    adaptive_applied: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ProviderStatusRow(RowModel):
    __json__ = ("capabilities",)
    provider: str
    installed: bool
    version: Optional[str] = None
    authenticated: Optional[bool] = None
    executable: Optional[str] = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    checked_at: str
    error: Optional[str] = None


class ProviderModelRow(RowModel):
    __json__ = ("capabilities",)
    provider: str
    model_id: str
    display_name: Optional[str] = None
    source: Literal["cli", "config", "static"]
    capabilities: list[str] = Field(default_factory=list)
    tier: Optional[str] = None
    verified_at: Optional[str] = None


Exactness = Literal["exact", "estimated", "unknown"]


class UsageSnapshot(RowModel):
    id: str
    provider: str
    model: Optional[str] = None
    state: str
    metric: str
    value: Optional[float] = None
    unit: Optional[str] = None
    exactness: Exactness
    source: str
    confidence: Optional[float] = None
    resets_at: Optional[str] = None
    observed_at: str


class Setting(RowModel):
    __json__ = ("value",)
    key: str
    value: Any
    updated_at: str


class KnowledgeDoc(RowModel):
    id: str
    project_id: Optional[str] = None
    kind: Literal["requirement", "adr", "doc", "task", "error_resolution", "code_meta", "skill", "model_history"]
    title: str
    body: str
    source_path: Optional[str] = None
    source_hash: str
    updated_at: str


class Skill(RowModel):
    __json__ = ("install_method", "compat", "dependencies", "permissions", "security", "project_scopes")
    id: str
    name: str
    category: Optional[str] = None
    kind: str
    source_url: Optional[str] = None
    source_verified: bool = False
    uncertainty: Optional[str] = None
    description: str = ""
    install_method: dict[str, Any] = Field(default_factory=dict)
    compat: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[Any] = Field(default_factory=list)
    permissions: list[Any] = Field(default_factory=list)
    security: dict[str, Any] = Field(default_factory=dict)
    state: str
    pinned_ref: Optional[str] = None
    installed_ref: Optional[str] = None
    previous_ref: Optional[str] = None
    local_path: Optional[str] = None
    project_scopes: list[str] = Field(default_factory=list)
    updated_at: str


class SkillEvent(RowModel):
    id: str
    skill_id: str
    action: str
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    ref: Optional[str] = None
    detail: Optional[str] = None
    ts: str


# --------------------------------------------------------------------------- #
# Handoff (short-term task context for an incoming agent)
# --------------------------------------------------------------------------- #


class GitSummary(BaseModel):
    branch: Optional[str] = None
    base_commit: Optional[str] = None
    head_commit: Optional[str] = None
    commits: list[str] = Field(default_factory=list)  # "sha subject", newest first, bounded
    diff_stat: str = ""
    changed_files: list[str] = Field(default_factory=list)
    uncommitted_changes: bool = False


class TestStatus(BaseModel):
    __test__ = False  # not a pytest class
    status: Optional[VerificationStatus] = None
    failed_checks: list[str] = Field(default_factory=list)
    failure_output: str = ""  # truncated, redacted


class Handoff(BaseModel):
    task_id: str
    objective: str
    requirements: list[str] = Field(default_factory=list)
    completed_work: list[str] = Field(default_factory=list)
    remaining_work: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    relevant_files: list[str] = Field(default_factory=list)
    git: GitSummary = Field(default_factory=GitSummary)
    test_status: TestStatus = Field(default_factory=TestStatus)
    known_issues: list[str] = Field(default_factory=list)
    next_action: str = ""

    def render(self) -> str:
        """Concise plain-text rendering for a provider prompt."""

        def block(title: str, items: list[str]) -> str:
            if not items:
                return ""
            return f"## {title}\n" + "\n".join(f"- {i}" for i in items) + "\n\n"

        out = f"## Objective\n{self.objective}\n\n"
        out += block("Requirements / acceptance criteria", self.requirements)
        out += block("Completed work", self.completed_work)
        out += block("Remaining work", self.remaining_work)
        out += block("Decisions", self.decisions)
        out += block("Relevant files", self.relevant_files)
        g = self.git
        if g.branch or g.commits or g.changed_files:
            out += "## Git\n"
            out += f"branch: {g.branch or '-'}  base: {g.base_commit or '-'}  head: {g.head_commit or '-'}\n"
            if g.commits:
                out += "commits:\n" + "\n".join(f"  {c}" for c in g.commits) + "\n"
            if g.diff_stat:
                out += f"diff stat:\n{g.diff_stat}\n"
            out += "\n"
        ts = self.test_status
        if ts.status:
            out += f"## Test status\n{ts.status}\n"
            if ts.failed_checks:
                out += "failed: " + ", ".join(ts.failed_checks) + "\n"
            if ts.failure_output:
                out += f"output:\n{ts.failure_output}\n"
            out += "\n"
        out += block("Known issues", self.known_issues)
        if self.next_action:
            out += f"## Next action\n{self.next_action}\n"
        return out


# --------------------------------------------------------------------------- #
# API DTOs
# --------------------------------------------------------------------------- #


class Measured(BaseModel):
    """A number that may be estimated or missing (ARCHITECTURE §6)."""

    value: Optional[float] = None
    exactness: Literal["exact", "measured", "estimated", "ai_evaluated", "unknown", "missing"] = "unknown"
    source: str = ""


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=1000)
    default_branch: Optional[str] = None
    verify_commands: list[VerifyCommand] = Field(default_factory=list)
    skill_profile: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    default_branch: Optional[str] = None
    verify_commands: Optional[list[VerifyCommand]] = None
    skill_profile: Optional[dict[str, Any]] = None


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=100_000)
    task_type: str = "coding"
    priority: int = Field(default=50, ge=0, le=100)
    objective_id: Optional[str] = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    verify_commands: list[VerifyCommand] = Field(default_factory=list)
    plan: Optional[Any] = None
    pending_steps: list[Any] = Field(default_factory=list)
    relevant_files: list[str] = Field(default_factory=list)
    role: Optional[str] = None
    preferred_provider: Optional[str] = None
    preferred_model: Optional[str] = None
    depends_on: list[str] = Field(default_factory=list)
    autostart: bool = False


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    description: Optional[str] = Field(default=None, max_length=100_000)
    task_type: Optional[str] = None
    priority: Optional[int] = Field(default=None, ge=0, le=100)
    acceptance_criteria: Optional[list[str]] = None
    verify_commands: Optional[list[VerifyCommand]] = None
    plan: Optional[Any] = None
    pending_steps: Optional[list[Any]] = None
    relevant_files: Optional[list[str]] = None
    next_action: Optional[str] = None
    role: Optional[str] = None
    preferred_provider: Optional[str] = None
    preferred_model: Optional[str] = None
    depends_on: Optional[list[str]] = None


class ApprovalResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    note: Optional[str] = Field(default=None, max_length=5000)


class SettingsUpdate(BaseModel):
    """Top-level policy sections to override, e.g. {"limits": {"max_parallel_workers": 2}}."""

    model_config = ConfigDict(extra="forbid")
    values: dict[str, Any]

    @field_validator("values")
    @classmethod
    def _known_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        allowed = {"limits", "budgets", "approvals", "worker_permissions", "review", "leadership", "security"}
        unknown = set(v) - allowed
        if unknown:
            raise ValueError(f"unknown settings keys: {sorted(unknown)}")
        if "security" in v:
            sec = v["security"]
            if not isinstance(sec, dict) or set(sec) - {"allowed_origins"}:
                raise ValueError("only security.allowed_origins may be overridden at runtime")
        wp = v.get("worker_permissions")
        if isinstance(wp, dict) and "claude_shell" in wp:
            # SECURITY_REVIEW #1/#3: an agent holding the API token must not be able to give itself a shell.
            raise ValueError("worker_permissions.claude_shell can only be set in config/policies.yaml")
        return v


class AgentInspector(BaseModel):
    id: str
    name: str
    role: str
    room: str
    activity: AgentActivity
    status: str
    provider: Optional[str] = None
    model: Optional[str] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    task_id: Optional[str] = None
    task_title: Optional[str] = None
    task_state: Optional[str] = None
    run_id: Optional[str] = None
    operation: Optional[str] = None
    since: str
    elapsed_seconds: Measured
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    test_results: Optional[dict[str, Any]] = None
    usage: dict[str, Any] = Field(default_factory=dict)
