"""Bounded adaptive score adjustment from verified history (ARCHITECTURE section 9, step 4). Pure."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Adjustment:
    points: float
    applied: bool
    explanation: str


def compute_adjustment(
    stats: Mapping[str, int] | None,
    *,
    min_samples: int,
    max_adjustment: float,
    enabled: bool = True,
    baseline: float = 0.5,
) -> Adjustment:
    """Points in [-max_adjustment, +max_adjustment]; zero below the evidence threshold."""
    if not enabled:
        return Adjustment(0.0, False, "adaptive routing disabled")
    runs = int((stats or {}).get("verified_runs", 0))
    successes = min(int((stats or {}).get("successes", 0)), runs)
    if runs < min_samples:
        return Adjustment(0.0, False, f"insufficient evidence (n < min_samples) [n={runs}, min_samples={min_samples}]")
    rate = successes / runs
    span = max(baseline, 1.0 - baseline)
    points = (rate - baseline) / span * max_adjustment
    points = round(max(-max_adjustment, min(max_adjustment, points)), 6)
    return Adjustment(
        points,
        True,
        f"{successes}/{runs} verified successes ({rate:.0%}) vs baseline {baseline:.0%}: "
        f"{points:+.2f} points (bound +/-{max_adjustment:g})",
    )
