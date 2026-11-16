"""Deterministic supervisor decisions (ARCHITECTURE §8). Pure functions of
task counters, the failed run's result and policy limits — no I/O."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal, Optional

from pydantic import BaseModel

from orchestrator.config import Policies
from orchestrator.domain.models import Task
from orchestrator.domain.states import TaskState
from orchestrator.providers.base import ErrorCategory, ExecutionResult
from orchestrator.util import parse_iso, to_iso, utcnow

S = TaskState
StopMode = Literal["cancel", "pause", "shutdown"]

MAX_QUOTA_WAIT = timedelta(hours=24)
MAX_BACKOFF_SECONDS = 300.0
QUOTA_CATEGORIES = frozenset({ErrorCategory.RATE_LIMIT, ErrorCategory.QUOTA_EXHAUSTED})
NON_RETRYABLE = frozenset({ErrorCategory.INVALID_REQUEST})


class SupervisorDecision(BaseModel):
    to_state: TaskState
    reason: str
    not_before: Optional[str] = None
    approval_kind: Optional[str] = None
    retry_count: Optional[int] = None
    consecutive_failures: Optional[int] = None
    counts_as_failure: bool = False


class Supervisor:
    def __init__(self, policies: Policies):
        self.p = policies

    # ---- limits ------------------------------------------------------------
    @property
    def max_runtime(self) -> float:
        return float(self.p.limit("max_active_runtime_seconds"))

    @property
    def heartbeat_timeout(self) -> float:
        return float(self.p.limit("run_heartbeat_timeout_seconds"))

    @property
    def max_repair_cycles(self) -> int:
        return self.p.int_limit("max_repair_cycles")

    def repair_allowed(self, task: Task) -> bool:
        return task.repair_cycles < self.max_repair_cycles

    def quota_wait_until(self, resets_at: Optional[str]) -> str:
        now = utcnow()
        reset = parse_iso(resets_at) if resets_at else None
        if reset is not None and now < reset <= now + MAX_QUOTA_WAIT:
            return to_iso(reset)
        return to_iso(now + timedelta(seconds=float(self.p.limit("quota_recheck_seconds"))))

    # ---- decisions ---------------------------------------------------------
    def on_stop(self, mode: StopMode) -> SupervisorDecision:
        if mode == "cancel":
            return SupervisorDecision(to_state=S.CANCELLED, reason="cancelled by user")
        if mode == "pause":
            return SupervisorDecision(to_state=S.PAUSED, reason="paused by user")
        return SupervisorDecision(to_state=S.QUEUED, reason="orchestrator shutdown; requeued")

    def on_run_failure(
        self, task: Task, result: ExecutionResult, *, resets_at: Optional[str] = None
    ) -> SupervisorDecision:
        cat = result.error_category or ErrorCategory.UNKNOWN
        msg = (result.error_message or cat.value)[:500]
        if cat in QUOTA_CATEGORIES:
            return SupervisorDecision(
                to_state=S.WAITING_FOR_QUOTA, reason=f"{cat.value}: {msg}", not_before=self.quota_wait_until(resets_at)
            )
        if cat == ErrorCategory.AUTH:
            return SupervisorDecision(to_state=S.BLOCKED, reason=f"provider authentication failed: {msg}")
        if cat == ErrorCategory.PERMISSION_DENIED:
            return SupervisorDecision(
                to_state=S.NEEDS_APPROVAL, reason=f"permission denied: {msg}", approval_kind="permission_escalation"
            )
        if cat == ErrorCategory.CANCELLED:
            # Cancelled without a user stop request (provider side): park it, do not retry blindly.
            return SupervisorDecision(to_state=S.PAUSED, reason=f"run cancelled outside the orchestrator: {msg}")

        retry = task.retry_count + 1
        consec = task.consecutive_failures + 1
        if cat in NON_RETRYABLE:
            return SupervisorDecision(
                to_state=S.FAILED, reason=f"{cat.value} (not retryable): {msg}", retry_count=retry,
                consecutive_failures=consec, counts_as_failure=True,
            )
        if retry > self.p.int_limit("max_retry_attempts") or consec >= self.p.int_limit("max_consecutive_failures"):
            return SupervisorDecision(
                to_state=S.FAILED,
                reason=f"{cat.value}: {msg} (retry limit reached: retries={retry - 1}, consecutive failures={consec})",
                retry_count=retry,
                consecutive_failures=consec,
                counts_as_failure=True,
            )
        backoff = min(MAX_BACKOFF_SECONDS, float(self.p.limit("retry_backoff_seconds")) * (2 ** (retry - 1)))
        return SupervisorDecision(
            to_state=S.QUEUED,
            reason=f"{cat.value}: {msg} (retry {retry})",
            not_before=to_iso(utcnow() + timedelta(seconds=backoff)) if backoff > 0 else None,
            retry_count=retry,
            consecutive_failures=consec,
            counts_as_failure=True,
        )
