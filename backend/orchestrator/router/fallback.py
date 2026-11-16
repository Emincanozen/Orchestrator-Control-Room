"""Failure-aware fallback planning on top of the router (ARCHITECTURE section 9, ADR on fallback).

Each ``ErrorCategory`` has its own rule; nothing is treated as quota by default.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..providers.base import ErrorCategory
from .router import ExcludedCandidate, Router, RoutingDecision, RoutingRequest

Action = Literal["retry_same", "resume_same", "switch", "wait_for_quota", "needs_approval", "block", "fail"]


class AttemptInfo(BaseModel):
    same_candidate_retries: int = 0  # retries already spent on the previous candidate
    provider_switches: int = 0  # provider switches already done for this task
    session_id: str | None = None  # resumable session of the failed run, if any
    max_same_retries: int = 2
    unknown_retries: int = 1  # UNKNOWN errors get a single blind retry


class FallbackPlan(BaseModel):
    action: Action
    reason: str
    decision: RoutingDecision | None = None
    excluded: list[ExcludedCandidate] = Field(default_factory=list)  # cumulative, for the caller to persist
    category: str = ""


def _plan(action: Action, reason: str, cat: ErrorCategory, **kw) -> FallbackPlan:
    return FallbackPlan(action=action, reason=reason, category=cat.value, **kw)


def plan_fallback(
    previous_decision: RoutingDecision,
    failure_category: ErrorCategory,
    attempt_info: AttemptInfo,
    request: RoutingRequest,
    router: Router,
) -> FallbackPlan:
    cat = failure_category
    cfg = router.fallback_cfg
    prov, model = previous_decision.provider, previous_decision.model
    if prov is None or model is None:
        return _plan("block", "previous decision has no chosen candidate to fall back from", cat)

    if cat == ErrorCategory.CANCELLED:
        return _plan("fail", "cancelled by the user/supervisor: no fallback", cat)
    if cat == ErrorCategory.PERMISSION_DENIED:
        return _plan("needs_approval", f"{prov}/{model} needed a permission it was not granted: ask for approval", cat)

    transient = cat in (ErrorCategory.NETWORK, ErrorCategory.TIMEOUT, ErrorCategory.CRASHED, ErrorCategory.TOOL_FAILURE)
    if cat == ErrorCategory.UNKNOWN:
        limit = attempt_info.unknown_retries
    else:
        limit = attempt_info.max_same_retries
    if (transient or cat == ErrorCategory.UNKNOWN) and attempt_info.same_candidate_retries < limit:
        sid = attempt_info.session_id
        n = attempt_info.same_candidate_retries + 1
        if sid:
            return _plan("resume_same", f"{cat.value}: resume session {sid} on {prov}/{model} (retry {n}/{limit})", cat,
                         decision=previous_decision)
        return _plan("retry_same", f"{cat.value}: retry {prov}/{model} (retry {n}/{limit})", cat,
                     decision=previous_decision)

    # --- everything below needs an alternative candidate -------------------------------------------------
    whole_provider = cat in (ErrorCategory.RATE_LIMIT, ErrorCategory.QUOTA_EXHAUSTED, ErrorCategory.AUTH)
    excluded = list(request.excluded)
    if cfg["exclude_failed_candidate"]:
        if whole_provider:
            excluded.append(ExcludedCandidate(provider=prov, model=None, reason=f"{cat.value} on {prov}"))
        else:
            excluded.append(ExcludedCandidate(provider=prov, model=model, reason=f"{cat.value} on {prov}/{model}"))

    if attempt_info.provider_switches >= cfg["max_provider_switches_per_task"]:
        # a switch would exceed the cap; quota still allows waiting for the same provider
        if cat in (ErrorCategory.RATE_LIMIT, ErrorCategory.QUOTA_EXHAUSTED):
            return _plan("wait_for_quota", f"provider switch limit ({cfg['max_provider_switches_per_task']}) reached; "
                         f"waiting for {prov} quota", cat, excluded=excluded)
        return _plan("block", f"provider switch limit ({cfg['max_provider_switches_per_task']}) reached after {cat.value}",
                     cat, excluded=excluded)

    new_req = request.model_copy(update={
        "excluded": excluded,
        "allow_lower_tier": cfg["allow_lower_tier"],
    })
    alt = router.route(new_req)
    if alt.provider is not None:
        return _plan("switch", f"{cat.value}: switching {prov}/{model} -> {alt.provider}/{alt.model}", cat,
                     decision=alt, excluded=excluded)

    if cat in (ErrorCategory.RATE_LIMIT, ErrorCategory.QUOTA_EXHAUSTED):
        return _plan("wait_for_quota", f"{cat.value} on {prov} and no suitable alternative: wait for quota", cat,
                     decision=alt, excluded=excluded)
    return _plan("block", f"{cat.value} on {prov}/{model} and no suitable alternative: {alt.no_candidate_reason}", cat,
                 decision=alt, excluded=excluded)
