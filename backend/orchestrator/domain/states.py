"""Task state machine. This file is a binding contract (see docs/ARCHITECTURE.md §4)."""

from __future__ import annotations

from enum import Enum


class TaskState(str, Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    TESTING = "TESTING"
    REVIEWING = "REVIEWING"
    WAITING_FOR_QUOTA = "WAITING_FOR_QUOTA"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


S = TaskState

TERMINAL_STATES: frozenset[TaskState] = frozenset({S.COMPLETED, S.CANCELLED})

# States in which a provider process may be alive for the task.
ACTIVE_STATES: frozenset[TaskState] = frozenset({S.PLANNING, S.RUNNING, S.TESTING, S.REVIEWING})

ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    S.CREATED: frozenset({S.QUEUED, S.CANCELLED}),
    S.QUEUED: frozenset({S.PLANNING, S.RUNNING, S.PAUSED, S.BLOCKED, S.NEEDS_APPROVAL, S.CANCELLED}),
    S.PLANNING: frozenset(
        {S.RUNNING, S.QUEUED, S.WAITING_FOR_QUOTA, S.NEEDS_APPROVAL, S.PAUSED, S.BLOCKED, S.FAILED, S.CANCELLED}
    ),
    S.RUNNING: frozenset(
        {S.TESTING, S.QUEUED, S.WAITING_FOR_QUOTA, S.NEEDS_APPROVAL, S.PAUSED, S.BLOCKED, S.FAILED, S.CANCELLED}
    ),
    S.TESTING: frozenset(
        {S.REVIEWING, S.RUNNING, S.COMPLETED, S.QUEUED, S.NEEDS_APPROVAL, S.PAUSED, S.BLOCKED, S.FAILED, S.CANCELLED}
    ),
    S.REVIEWING: frozenset(
        {S.COMPLETED, S.RUNNING, S.QUEUED, S.WAITING_FOR_QUOTA, S.NEEDS_APPROVAL, S.PAUSED, S.BLOCKED, S.FAILED, S.CANCELLED}
    ),
    S.WAITING_FOR_QUOTA: frozenset({S.QUEUED, S.PAUSED, S.FAILED, S.CANCELLED}),
    S.NEEDS_APPROVAL: frozenset({S.QUEUED, S.COMPLETED, S.BLOCKED, S.FAILED, S.CANCELLED}),
    S.PAUSED: frozenset({S.QUEUED, S.CANCELLED}),
    S.BLOCKED: frozenset({S.QUEUED, S.FAILED, S.CANCELLED}),
    S.FAILED: frozenset({S.QUEUED, S.CANCELLED}),  # explicit retry only
    S.COMPLETED: frozenset(),
    S.CANCELLED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, current: TaskState, target: TaskState):
        super().__init__(f"Illegal task transition {current.value} -> {target.value}")
        self.current = current
        self.target = target


def can_transition(current: TaskState, target: TaskState) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def assert_transition(current: TaskState, target: TaskState) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)


class AgentActivity(str, Enum):
    """Visual activity of an office seat. Derived only from recorded events."""

    OFFLINE = "offline"
    IDLE = "idle"  # waiting, no process
    PLANNING = "planning"
    CODING = "coding"
    RESEARCHING = "researching"
    TESTING = "testing"
    REVIEWING = "reviewing"
    CONSULTING = "consulting"
    RATE_LIMITED = "rate_limited"
    NEEDS_APPROVAL = "needs_approval"
    BLOCKED = "blocked"
    CELEBRATING = "celebrating"  # transient after verified completion


class RunStatus(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"  # process died / app crashed; found by recovery


class RunPhase(str, Enum):
    PLAN = "plan"
    EXECUTE = "execute"
    REPAIR = "repair"
    REVIEW = "review"
    ADVISE = "advise"


class UsageState(str, Enum):
    AVAILABLE = "AVAILABLE"
    LOW = "LOW"
    EXHAUSTED = "EXHAUSTED"
    COOLDOWN = "COOLDOWN"
    UNKNOWN = "UNKNOWN"
    AUTH_ERROR = "AUTH_ERROR"
    PROVIDER_ERROR = "PROVIDER_ERROR"


class SkillState(str, Enum):
    CATALOGUED_ON_DEMAND = "CATALOGUED_ON_DEMAND"
    DOWNLOADED_AND_DISABLED = "DOWNLOADED_AND_DISABLED"
    INSTALLED_AND_ACTIVE = "INSTALLED_AND_ACTIVE"
    INSTALL_FAILED = "INSTALL_FAILED"
    INCOMPATIBLE = "INCOMPATIBLE"
    NEEDS_SECURITY_REVIEW = "NEEDS_SECURITY_REVIEW"
