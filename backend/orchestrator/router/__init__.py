"""Capability-aware routing engine. Pure logic: no db/api/scheduler/services imports."""

from .adaptive import Adjustment, compute_adjustment
from .capabilities import (
    ALL_CAPABILITIES,
    CORE_CAPABILITIES,
    TIER_ORDER,
    VISUAL_CAPABILITIES,
    CapabilityRegistry,
    ModelEntry,
    max_tier,
    tier_rank,
)
from .classifier import TaskClassification, classify
from .fallback import AttemptInfo, FallbackPlan, plan_fallback
from .router import (
    ExcludedCandidate,
    LeaderIdentity,
    PreviousRun,
    RankedCandidate,
    RejectedCandidate,
    Router,
    RoutingDecision,
    RoutingRequest,
    UserPreferences,
)

__all__ = [
    "ALL_CAPABILITIES", "CORE_CAPABILITIES", "TIER_ORDER", "VISUAL_CAPABILITIES", "Adjustment", "AttemptInfo",
    "CapabilityRegistry", "ExcludedCandidate", "FallbackPlan", "LeaderIdentity", "ModelEntry", "PreviousRun",
    "RankedCandidate", "RejectedCandidate", "Router", "RoutingDecision", "RoutingRequest", "TaskClassification",
    "UserPreferences", "classify", "compute_adjustment", "max_tier", "plan_fallback", "tier_rank",
]
