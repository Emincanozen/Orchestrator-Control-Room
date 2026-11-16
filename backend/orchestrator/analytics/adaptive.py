"""Adaptive-routing report and settings.

Effective adaptive config = config/routing.yaml `adaptive` overlaid by settings key `routing.adaptive`
({enabled?, min_samples?, max_adjustment?}). The routing service should build its Router config with
`effective_adaptive_cfg(records.settings_all(tx), routing_yaml_cfg)` (see that function). Read-only here
except `save_settings`, which only writes the `routing.adaptive` settings row.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from orchestrator.analytics.metrics import metric, missing
from orchestrator.db import records, telemetry
from orchestrator.db.database import Tx
from orchestrator.router.adaptive import compute_adjustment
from orchestrator.router.router import DEFAULT_WEIGHTS
from orchestrator.util import loads

SETTINGS_KEY = "routing.adaptive"
BOUNDS = {"min_samples": (3, 500), "max_adjustment": (0.0, 15.0)}
DEFAULTS = {"enabled": True, "min_samples": 20, "max_adjustment": 10.0}


def effective_adaptive_cfg(settings: Mapping[str, Any], routing_cfg: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Merge yaml `adaptive` with the settings override; out-of-range override values are ignored (yaml/default wins).
    Returns the dict the Router expects as cfg["adaptive"]: {enabled, min_samples, max_adjustment} (+ explain)."""
    base = dict(DEFAULTS)
    base.update({k: v for k, v in ((routing_cfg or {}).get("adaptive") or {}).items() if k in (*DEFAULTS, "explain")})
    ov = settings.get(SETTINGS_KEY)
    if isinstance(ov, Mapping):
        if isinstance(ov.get("enabled"), bool):
            base["enabled"] = ov["enabled"]
        ms = ov.get("min_samples")
        if isinstance(ms, int) and not isinstance(ms, bool) and BOUNDS["min_samples"][0] <= ms <= BOUNDS["min_samples"][1]:
            base["min_samples"] = ms
        ma = ov.get("max_adjustment")
        if isinstance(ma, (int, float)) and not isinstance(ma, bool) and BOUNDS["max_adjustment"][0] <= ma <= BOUNDS["max_adjustment"][1]:
            base["max_adjustment"] = float(ma)
    return base


def settings_view(settings: Mapping[str, Any], routing_cfg: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    eff = effective_adaptive_cfg(settings, routing_cfg)
    ov = settings.get(SETTINGS_KEY) if isinstance(settings.get(SETTINGS_KEY), Mapping) else {}
    scoring = {**DEFAULT_WEIGHTS, **{k: float(v) for k, v in ((routing_cfg or {}).get("scoring") or {}).items()}}
    return {
        "enabled": bool(eff["enabled"]), "min_samples": int(eff["min_samples"]), "max_adjustment": float(eff["max_adjustment"]),
        "overrides": {k: ov[k] for k in ("enabled", "min_samples", "max_adjustment") if k in ov},
        "bounds": {"min_samples": list(BOUNDS["min_samples"]), "max_adjustment": list(BOUNDS["max_adjustment"])},
        "effective_cap_points": _cap(eff, scoring),
        "note": "the router caps the adjustment at min(max_adjustment, scoring.historical_success)",
    }


def _cap(eff: Mapping[str, Any], scoring: Mapping[str, float]) -> float:
    w = float(scoring.get("historical_success", 5.0))
    return min(float(eff["max_adjustment"]), w) if w > 0 else 0.0


def save_settings(tx: Tx, patch: Mapping[str, Any]) -> None:
    """Persist only the validated fields given in `patch` (merged over the existing override)."""
    cur = records.settings_all(tx).get(SETTINGS_KEY)
    merged = dict(cur) if isinstance(cur, Mapping) else {}
    merged.update({k: v for k, v in patch.items() if k in DEFAULTS})
    records.setting_put(tx, SETTINGS_KEY, merged)


def report(tx: Tx, settings: Mapping[str, Any], routing_cfg: Optional[Mapping[str, Any]] = None, *, history_limit: int = 50) -> dict[str, Any]:
    """Per (provider, model, task_type) cell: evidence, whether the threshold is met, and the exact adjustment the router
    would apply today (router/adaptive.compute_adjustment with the effective settings and the weight cap)."""
    eff = effective_adaptive_cfg(settings, routing_cfg)
    scoring = {**DEFAULT_WEIGHTS, **{k: float(v) for k, v in ((routing_cfg or {}).get("scoring") or {}).items()}}
    cap = _cap(eff, scoring)
    hist = telemetry.history_stats(tx)
    run_cells = {(r["provider"], r["model"], r["task_type"]): r["n"] for r in tx.all(
        "SELECT r.provider, r.model, t.task_type, COUNT(*) AS n FROM runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.phase IN ('execute','repair') AND r.model IS NOT NULL GROUP BY 1, 2, 3")}
    cells = []
    for key in sorted(set(hist) | set(run_cells)):
        p, m, t = key
        stats = hist.get(key, {"verified_runs": 0, "successes": 0})
        adj = compute_adjustment(stats, min_samples=int(eff["min_samples"]), max_adjustment=cap, enabled=bool(eff["enabled"]) and cap > 0)
        n = stats["verified_runs"]
        cells.append({
            "provider": p, "model": m, "task_type": t, "runs": run_cells.get(key, 0), "verified_runs": n, "successes": stats["successes"],
            "min_samples": int(eff["min_samples"]), "threshold_met": n >= int(eff["min_samples"]),
            "success_rate": (metric(round(stats["successes"] / n, 4), "measured", "db.telemetry.history_stats", n) if n
                             else missing("db.telemetry.history_stats", "no verified runs")),
            "adjustment": {"points": adj.points, "applied": adj.applied, "explanation": adj.explanation},
        })
    rows = tx.all("SELECT id, task_id, run_id, phase, chosen_provider, chosen_model, reasons, created_at FROM routing_decisions "
                  "WHERE adaptive_applied = 1 ORDER BY created_at DESC, id DESC LIMIT ?", (max(1, min(history_limit, 500)),))
    history = [{"id": r["id"], "task_id": r["task_id"], "run_id": r["run_id"], "phase": r["phase"], "provider": r["chosen_provider"],
                "model": r["chosen_model"], "created_at": r["created_at"],
                "adaptive_reasons": [x for x in (loads(r["reasons"], []) or []) if isinstance(x, str) and x.startswith("adaptive")]}
               for r in rows]
    return {"settings": settings_view(settings, routing_cfg), "cells": cells, "applied_history": history,
            "decisions_with_adaptive": tx.scalar("SELECT COUNT(*) FROM routing_decisions WHERE adaptive_applied = 1")}
