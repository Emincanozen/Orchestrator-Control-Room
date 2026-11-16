"""Usage monitor: stores provider observations as `usage_snapshots`, derives per-provider state,
emits `usage.updated`, and implements the scheduler's `quota_check`.

Rules (ADR-009): only what a provider actually emitted is stored; an unknown stays UNKNOWN; no
percentage is ever synthesised. The only mapping done here is representational:
  * exactness  "estimate" (usage package) -> "estimated" (DB / API),
  * confidence "high|medium|low" -> 0.9 | 0.6 | 0.3 in the REAL column and back (a category, not a measurement).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Optional

from orchestrator.db import runs as runs_repo
from orchestrator.db import telemetry
from orchestrator.db.database import Tx
from orchestrator.domain import events as ev
from orchestrator.domain import models as dm
from orchestrator.domain.models import Task
from orchestrator.domain.states import UsageState
from orchestrator.providers.base import AgentEvent, AgentEventKind, ExecutionResult, ProviderStatus
from orchestrator.services.context import AppContext
from orchestrator.usage import (
    ProviderUsageState,
    UsagePolicy,
    UsageSnapshot,
    derive_state,
    snapshot_from_status,
    snapshots_from_event,
    snapshots_from_result,
)
from orchestrator.util import new_id, parse_iso, to_iso

log = logging.getLogger(__name__)

BLOCKING_QUOTA_STATES = frozenset({UsageState.EXHAUSTED, UsageState.COOLDOWN})
_CONF_TO_FLOAT = {"high": 0.9, "medium": 0.6, "low": 0.3}


def _conf_label(v: Optional[float]) -> str:
    if v is None:
        return "low"
    return "high" if v >= 0.8 else "medium" if v >= 0.5 else "low"


def to_row(s: UsageSnapshot) -> dict[str, Any]:
    return {
        "id": new_id("usg"),
        "provider": s.provider,
        "model": s.model,
        "state": s.state.value,
        "metric": s.metric,
        "value": s.value,
        "unit": s.unit,
        "exactness": "estimated" if s.exactness == "estimate" else s.exactness,
        "source": s.source,
        "confidence": _CONF_TO_FLOAT.get(s.confidence),
        "resets_at": to_iso(s.resets_at) if s.resets_at else None,
        "observed_at": to_iso(s.observed_at),
    }


def from_row(r: dm.UsageSnapshot) -> UsageSnapshot:
    return UsageSnapshot(
        provider=r.provider, model=r.model, state=UsageState(r.state), metric=r.metric, value=r.value, unit=r.unit,
        exactness="estimate" if r.exactness == "estimated" else r.exactness,  # type: ignore[arg-type]
        source=r.source, confidence=_conf_label(r.confidence),  # type: ignore[arg-type]
        resets_at=parse_iso(r.resets_at), observed_at=parse_iso(r.observed_at),  # type: ignore[arg-type]
    )


def snapshot_json(s: UsageSnapshot) -> dict[str, Any]:
    """API/event shape of one stored observation."""
    return {
        "provider": s.provider,
        "model": s.model,
        "metric": s.metric,
        "value": s.value,
        "unit": s.unit,
        "exactness": "estimated" if s.exactness == "estimate" else s.exactness,
        "source": s.source,
        "confidence": s.confidence,
        "state": s.state.value,
        "resets_at": to_iso(s.resets_at) if s.resets_at else None,
        "observed_at": to_iso(s.observed_at),
    }


def state_json(st: ProviderUsageState) -> dict[str, Any]:
    return {
        "state": st.state.value,
        "reason": st.reason,
        "resets_at": to_iso(st.resets_at) if st.resets_at else None,
        "until": to_iso(st.until) if st.until else None,
        "confidence": st.confidence,
        "evidence_metric": st.evidence_metric,
    }


class UsageMonitor:
    def __init__(self, ctx: AppContext):
        self.ctx = ctx
        # set by the app: async fn(task) -> True when the router can place the task on some provider now
        self.alternative_finder: Optional[Callable[[Task], Awaitable[bool]]] = None

    # ---- reads ---------------------------------------------------------------
    @staticmethod
    def policy_tx(ctx: AppContext, tx: Tx) -> UsagePolicy:
        return UsagePolicy(recheck_seconds=float(ctx.policies_tx(tx).limit("quota_recheck_seconds")))

    def latest_tx(self, tx: Tx) -> list[UsageSnapshot]:
        return [from_row(r) for r in telemetry.snapshots_latest(tx)]

    def _states(self, snaps: list[UsageSnapshot], providers: Iterable[str], policy: UsagePolicy,
                now: Optional[datetime] = None) -> list[ProviderUsageState]:
        names = sorted(set(providers) | {s.provider for s in snaps})
        return [derive_state(snaps, provider=p, now=now, policy=policy) for p in names]

    async def states(self, extra_providers: Iterable[str] = ()) -> list[ProviderUsageState]:
        """Provider-wide current state for every provider with observations (+ `extra_providers`)."""
        def fn(tx: Tx) -> list[ProviderUsageState]:
            return self._states(self.latest_tx(tx), extra_providers, self.policy_tx(self.ctx, tx))

        return await self.ctx.db.read(fn)

    async def state_for(self, provider: str, model: Optional[str] = None) -> ProviderUsageState:
        def fn(tx: Tx) -> ProviderUsageState:
            return derive_state(self.latest_tx(tx), provider=provider, model=model, policy=self.policy_tx(self.ctx, tx))

        return await self.ctx.db.read(fn)

    async def report(self, extra_providers: Iterable[str] = ()) -> list[dict[str, Any]]:
        """Entries for GET /api/usage: one per provider and one per (provider, model) that has
        model-scoped observations; each carries the derived state and its latest values."""
        def fn(tx: Tx) -> list[dict[str, Any]]:
            snaps = self.latest_tx(tx)
            policy = self.policy_tx(self.ctx, tx)
            out: list[dict[str, Any]] = []
            for p in sorted(set(extra_providers) | {s.provider for s in snaps}):
                mine = [s for s in snaps if s.provider == p]
                out.append({
                    "provider": p, "model": None, **{"usage_state": state_json(derive_state(snaps, provider=p, policy=policy))},
                    "values": [snapshot_json(s) for s in mine if s.model is None],
                })
                for m in sorted({s.model for s in mine if s.model}):
                    out.append({
                        "provider": p, "model": m,
                        "usage_state": state_json(derive_state(snaps, provider=p, model=m, policy=policy)),
                        "values": [snapshot_json(s) for s in mine if s.model == m],
                    })
            return out

        return await self.ctx.db.read(fn)

    # ---- writes --------------------------------------------------------------
    async def record(self, snaps: list[UsageSnapshot], *, provider: str, cause: str) -> None:
        """Persist observations and emit one `usage.updated` (all in one transaction)."""
        if not snaps:
            return

        def fn(tx: Tx) -> None:
            for s in snaps:
                telemetry.snapshot_insert(tx, to_row(s))
            latest = self.latest_tx(tx)
            state = derive_state(latest, provider=provider, policy=self.policy_tx(self.ctx, tx))
            self.ctx.bus.append(
                tx, ev.USAGE_UPDATED,
                payload={
                    "provider": provider, "cause": cause, "usage_state": state_json(state),
                    "snapshots": [snapshot_json(s) for s in snaps],
                },
            )

        await self.ctx.db.write(fn)

    # UsageSink port (called by the executor)
    async def on_event(self, provider: str, model: Optional[str], event: AgentEvent) -> None:
        if event.kind not in (AgentEventKind.RATE_LIMIT, AgentEventKind.USAGE):
            return
        await self.record(snapshots_from_event(provider, event, model=model), provider=provider, cause=f"event:{event.kind.value}")

    async def on_result(self, provider: str, result: ExecutionResult, *, resets_at: Optional[str] = None) -> None:
        await self.record(snapshots_from_result(provider, result, retry_after=resets_at), provider=provider, cause="run_result")

    async def on_status(self, status: ProviderStatus) -> None:
        await self.record(snapshot_from_status(status), provider=status.provider, cause="detect")

    # ---- scheduler hook --------------------------------------------------------
    async def quota_check(self, task: Task) -> bool:
        """May a WAITING_FOR_QUOTA task leave that state? Yes when the provider it was waiting on is no
        longer EXHAUSTED/COOLDOWN, or when the router can place it on another provider."""
        runs = await self.ctx.db.read(lambda tx: runs_repo.for_task(tx, task.id))
        if not runs:
            return True
        last = runs[-1]
        state = await self.state_for(last.provider, last.model)
        if state.state not in BLOCKING_QUOTA_STATES:
            return True
        if self.alternative_finder is not None:
            try:
                return bool(await self.alternative_finder(task))
            except Exception:  # noqa: BLE001
                log.exception("alternative-candidate probe failed")
        return False
