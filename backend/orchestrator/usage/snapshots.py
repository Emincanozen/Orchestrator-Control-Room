"""Turn provider observations into usage snapshot records and derive a usage state.

Principles (ADR-009): store only what a provider actually emitted, with source,
exactness and confidence; never fabricate a percentage; unknown stays UNKNOWN.

Snapshot ``state`` is the state *implied by that single observation*. Pure
measurements (token counts, cost) carry ``UNKNOWN`` because they say nothing
about headroom. :func:`derive_state` combines observations at a point in time.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel

from ..domain.states import UsageState
from ..providers.base import AgentEvent, AgentEventKind, ErrorCategory, ExecutionResult, ProviderStatus

Exactness = Literal["exact", "estimate", "unknown"]
Confidence = Literal["high", "medium", "low"]

# Metrics that carry provider-state information (everything else is a plain measurement).
UTILIZATION_PREFIX = "window_utilization."
M_RATE_STATUS = "rate_limit_status"
M_PROVIDER_ERROR = "provider_error"
M_AUTH = "auth"
M_RUN_SUCCESS = "run_success"
M_AUTH_OK = "auth_ok"

_SEVERITY = {
    UsageState.EXHAUSTED: 6,
    UsageState.AUTH_ERROR: 5,
    UsageState.COOLDOWN: 4,
    UsageState.PROVIDER_ERROR: 3,
    UsageState.LOW: 2,
    UsageState.AVAILABLE: 1,
    UsageState.UNKNOWN: 0,
}


@dataclass(frozen=True)
class UsagePolicy:
    low_threshold: float = 0.8  # utilization >= this -> LOW
    recheck_seconds: float = 300.0  # policies.yaml limits.quota_recheck_seconds
    max_age_seconds: float = 6 * 3600.0  # AVAILABLE/LOW observations without a reset time go stale


class UsageSnapshot(BaseModel):
    provider: str
    model: str | None = None  # None = provider-wide observation
    state: UsageState = UsageState.UNKNOWN
    metric: str
    value: float | None = None
    unit: str | None = None
    exactness: Exactness = "unknown"
    source: str
    confidence: Confidence = "low"
    resets_at: datetime | None = None
    observed_at: datetime


class ProviderUsageState(BaseModel):
    provider: str
    model: str | None = None
    state: UsageState
    reason: str
    resets_at: datetime | None = None
    until: datetime | None = None  # when this state stops being trusted
    confidence: Confidence = "low"
    evidence_metric: str | None = None


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _to_dt(value: Any) -> datetime | None:
    """Accept epoch seconds/millis, ISO strings, datetimes. Anything else -> None (never guess)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:  # milliseconds
            v /= 1000.0
        if v <= 0:
            return None
        try:
            return datetime.fromtimestamp(v, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- creation


def snapshots_from_event(
    provider: str, event: AgentEvent, *, model: str | None = None, observed_at: datetime | None = None,
    policy: UsagePolicy = UsagePolicy(),
) -> list[UsageSnapshot]:
    """Snapshots for a RATE_LIMIT or USAGE event; other kinds produce none."""
    now = _utc(observed_at or datetime.now(UTC))
    d = event.data
    out: list[UsageSnapshot] = []
    if event.kind == AgentEventKind.RATE_LIMIT:
        resets = _to_dt(d.get("resets_at"))
        status = str(d.get("status") or "").lower()
        window = str(d.get("window") or "unknown")
        util = d.get("utilization")
        if isinstance(util, (int, float)) and not isinstance(util, bool):
            u = float(util)
            out.append(UsageSnapshot(
                provider=provider, model=None,
                state=(UsageState.EXHAUSTED if u >= 1.0
                       else UsageState.LOW if u >= policy.low_threshold else UsageState.AVAILABLE),
                metric=UTILIZATION_PREFIX + window, value=u, unit="ratio", exactness="exact",
                source=f"{provider}.rate_limit_event", confidence="high", resets_at=resets, observed_at=now))
        if status:
            if status.startswith("reject") or status in ("blocked", "exceeded"):
                st = UsageState.EXHAUSTED
            elif "warn" in status:
                st = UsageState.LOW
            elif status == "allowed":
                st = UsageState.AVAILABLE
            else:
                st = UsageState.UNKNOWN
            out.append(UsageSnapshot(
                provider=provider, model=None, state=st, metric=M_RATE_STATUS, value=None, unit=status,
                exactness="exact", source=f"{provider}.rate_limit_event", confidence="high",
                resets_at=resets, observed_at=now))
    elif event.kind == AgentEventKind.USAGE:
        out += _usage_measurements(provider, model, d, now)
    return out


def _usage_measurements(provider: str, model: str | None, d: dict[str, Any], now: datetime) -> list[UsageSnapshot]:
    out: list[UsageSnapshot] = []
    for key, val in d.items():
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        if key == "cost_usd":
            out.append(UsageSnapshot(
                provider=provider, model=model, metric="cost_usd", value=float(val),
                unit="usd_list_price_equivalent", exactness="estimate", source=f"{provider}.run_usage",
                confidence="medium", observed_at=now))  # subscription login: not money actually charged
        elif key.endswith("_tokens") or key == "num_turns":
            out.append(UsageSnapshot(
                provider=provider, model=model, metric=key, value=float(val),
                unit="turns" if key == "num_turns" else "tokens", exactness="exact",
                source=f"{provider}.run_usage", confidence="high", observed_at=now))
    return out


def error_scope(result: ExecutionResult) -> Literal["provider", "model"]:
    """Which scope a failed run's evidence has.

    * "model": only a quota/rate-limit failure whose own error text NAMES the model the run used (so the evidence
      is about that model, e.g. a per-model quota). Nothing else is ever inferred as model-scoped.
    * "provider": everything else - the default and the conservative choice: account-level limits, auth errors
      and network/crash errors say nothing specific about one model.
    """
    if result.error_category in (ErrorCategory.QUOTA_EXHAUSTED, ErrorCategory.RATE_LIMIT):
        model = (result.model or "").strip().lower()
        text = (result.error_message or "").lower()
        if len(model) >= 4 and model in text:
            return "model"
    return "provider"


def snapshots_from_result(
    provider: str, result: ExecutionResult, *, observed_at: datetime | None = None,
    policy: UsagePolicy = UsagePolicy(), retry_after: Any | None = None,
) -> list[UsageSnapshot]:
    """Snapshots from a finished run: token counts and the state implied by success / classified error.

    ``retry_after`` may carry a reset time parsed elsewhere (epoch/ISO/datetime); it is used only for
    quota/rate-limit errors and never invented.
    """
    now = _utc(observed_at or datetime.now(UTC))
    model = result.model
    out = _usage_measurements(provider, model, result.usage, now)
    cat = result.error_category
    if result.success:
        out.append(UsageSnapshot(provider=provider, model=model, state=UsageState.AVAILABLE, metric=M_RUN_SUCCESS,
                                 exactness="exact", source=f"{provider}.run_result", confidence="medium",
                                 observed_at=now))
        return out
    resets = _to_dt(retry_after)
    # Provider-scoped evidence is stored with model=None so it marks the whole provider; model-scoped evidence
    # (see error_scope) keeps the model and marks that model only.
    err_model = model if error_scope(result) == "model" else None
    base = dict(provider=provider, model=err_model, exactness="exact", source=f"{provider}.classified_error",
                confidence="medium", resets_at=resets, observed_at=now)
    if cat == ErrorCategory.QUOTA_EXHAUSTED:
        out.append(UsageSnapshot(state=UsageState.EXHAUSTED, metric=M_PROVIDER_ERROR, unit=cat.value, **base))
    elif cat == ErrorCategory.RATE_LIMIT:
        out.append(UsageSnapshot(state=UsageState.COOLDOWN, metric=M_PROVIDER_ERROR, unit=cat.value, **base))
    elif cat == ErrorCategory.AUTH:
        base["resets_at"] = None
        out.append(UsageSnapshot(state=UsageState.AUTH_ERROR, metric=M_AUTH, unit=cat.value, **base))
    elif cat in (ErrorCategory.NETWORK, ErrorCategory.CRASHED):
        base["resets_at"] = None
        base["confidence"] = "low"
        out.append(UsageSnapshot(state=UsageState.PROVIDER_ERROR, metric=M_PROVIDER_ERROR, unit=cat.value, **base))
    # timeout, invalid_request, tool_failure, permission_denied, cancelled, unknown: no provider-state evidence
    return out


def snapshot_from_status(status: ProviderStatus, *, observed_at: datetime | None = None) -> list[UsageSnapshot]:
    """detect() outcome: not installed -> PROVIDER_ERROR, auth False -> AUTH_ERROR, auth True -> clears AUTH_ERROR."""
    now = _utc(observed_at or datetime.now(UTC))
    src = f"{status.provider}.detect"
    if not status.installed:
        return [UsageSnapshot(provider=status.provider, state=UsageState.PROVIDER_ERROR, metric=M_PROVIDER_ERROR,
                              unit="not_installed", exactness="exact", source=src, confidence="high", observed_at=now)]
    if status.authenticated is False:
        return [UsageSnapshot(provider=status.provider, state=UsageState.AUTH_ERROR, metric=M_AUTH, unit="auth",
                              exactness="exact", source=src, confidence="high", observed_at=now)]
    if status.authenticated is True:
        return [UsageSnapshot(provider=status.provider, state=UsageState.UNKNOWN, metric=M_AUTH_OK, exactness="exact",
                              source=src, confidence="high", observed_at=now)]
    return []


# --------------------------------------------------------------------------- derivation


def _expiry(o: UsageSnapshot, policy: UsagePolicy) -> datetime | None:
    """When an observation stops being trusted. None = never (only for sticky AUTH_ERROR)."""
    at = _utc(o.observed_at)
    resets = _utc(o.resets_at) if o.resets_at else None
    if o.state in (UsageState.EXHAUSTED, UsageState.COOLDOWN):
        return resets or at + timedelta(seconds=policy.recheck_seconds)
    if o.state == UsageState.PROVIDER_ERROR:
        return at + timedelta(seconds=policy.recheck_seconds)
    if o.state == UsageState.AUTH_ERROR:
        return None
    fallback = at + timedelta(seconds=policy.max_age_seconds)  # AVAILABLE / LOW
    return min(resets, fallback) if resets else fallback


def derive_state(
    observations: Iterable[UsageSnapshot], *, provider: str, model: str | None = None,
    now: datetime | None = None, policy: UsagePolicy = UsagePolicy(),
) -> ProviderUsageState:
    """Current state for a provider (optionally a model) from stored snapshots.

    * An EXHAUSTED/COOLDOWN observation holds until its reset time, then the state is UNKNOWN (not
      AVAILABLE) until something re-observes it. Without a reset time it holds for ``recheck_seconds``.
    * A later successful run disproves EXHAUSTED/COOLDOWN/PROVIDER_ERROR/AUTH_ERROR.
    * AUTH_ERROR is sticky until a later success or a later positive auth check.
    * The worst non-expired state across metrics wins; no evidence means UNKNOWN.
    """
    now = _utc(now or datetime.now(UTC))
    # Scope: a model query sees provider-wide observations plus that model's own; a provider-wide query sees only
    # provider-wide observations (a model-scoped quota must not mark the whole provider) - except successes, which
    # come from any model and still prove the account works.
    rel = [o for o in observations if o.provider == provider
           and (o.model is None or o.model == model or (model is None and o.metric == M_RUN_SUCCESS))]
    successes = [_utc(o.observed_at) for o in rel if o.metric == M_RUN_SUCCESS]
    auth_oks = [_utc(o.observed_at) for o in rel if o.metric == M_AUTH_OK]
    last_success = max(successes) if successes else None
    last_auth_ok = max(auth_oks) if auth_oks else None

    latest: dict[str, UsageSnapshot] = {}
    for o in rel:
        if o.metric in (M_RUN_SUCCESS, M_AUTH_OK):
            continue
        cur = latest.get(o.metric)
        if cur is None or _utc(o.observed_at) >= _utc(cur.observed_at):
            latest[o.metric] = o

    candidates: list[tuple[UsageSnapshot, datetime | None]] = []
    for o in latest.values():
        if o.state == UsageState.UNKNOWN:
            continue
        exp = _expiry(o, policy)
        if exp is not None and now >= exp:
            continue
        at = _utc(o.observed_at)
        if o.state in (UsageState.EXHAUSTED, UsageState.COOLDOWN, UsageState.PROVIDER_ERROR, UsageState.AUTH_ERROR):
            if last_success and last_success > at:
                continue
        if o.state == UsageState.AUTH_ERROR and last_auth_ok and last_auth_ok > at:
            continue
        candidates.append((o, exp))

    if candidates:
        o, exp = max(candidates, key=lambda c: (_SEVERITY[c[0].state], _utc(c[0].observed_at)))
        return ProviderUsageState(
            provider=provider, model=model, state=o.state, reason=_reason(o), resets_at=o.resets_at, until=exp,
            confidence=o.confidence, evidence_metric=o.metric)
    if last_success and (now - last_success).total_seconds() <= policy.max_age_seconds:
        return ProviderUsageState(
            provider=provider, model=model, state=UsageState.AVAILABLE, reason="a run succeeded recently (headroom unknown)",
            until=last_success + timedelta(seconds=policy.max_age_seconds), confidence="low", evidence_metric=M_RUN_SUCCESS)
    return ProviderUsageState(provider=provider, model=model, state=UsageState.UNKNOWN,
                              reason="no current observation", confidence="low")


def _reason(o: UsageSnapshot) -> str:
    if o.metric.startswith(UTILIZATION_PREFIX) and o.value is not None:
        return f"{o.metric[len(UTILIZATION_PREFIX):]} window at {o.value:.0%} (source {o.source})"
    if o.metric == M_RATE_STATUS:
        return f"rate limit status {o.unit} (source {o.source})"
    return f"{o.unit or o.metric} (source {o.source})"
