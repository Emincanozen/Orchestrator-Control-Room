"""System routes: health (no auth), status, settings."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends

from orchestrator import __version__
from orchestrator.api.deps import AUTH, get_rt
from orchestrator.api.runtime import Runtime
from orchestrator.api.security import effective_origins
from orchestrator.config import deep_merge, default_policies
from orchestrator.db import records
from orchestrator.db.database import Tx
from orchestrator.domain.models import SettingsUpdate
from orchestrator.errors import ValidationFailed

public = APIRouter(prefix="/api", tags=["system"])
router = APIRouter(prefix="/api", tags=["system"], dependencies=AUTH)

_ZERO_OK_LIMITS = {"retry_backoff_seconds", "max_consultations_per_task", "max_repair_cycles", "max_retry_attempts"}


@public.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


@router.get("/status")
async def status(rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    # Read the default-leader progress BEFORE the snapshot: if it says "detecting" and the leader row appears
    # meanwhile the answer is "set"; it can never claim "none" for a default that is still being recorded.
    stack = rt.extras.get("leader")
    default_state = getattr(getattr(stack, "leadership", None), "default_state", "idle")

    def fn(tx: Tx) -> dict[str, Any]:
        counts = {r[0]: r[1] for r in tx.all("SELECT state, COUNT(*) FROM tasks GROUP BY state")}
        pending = tx.scalar("SELECT COUNT(*) FROM approvals WHERE status = 'pending'")
        live_runs = tx.scalar("SELECT COUNT(*) FROM runs WHERE status IN ('starting','running')")
        last_seq = tx.scalar("SELECT COALESCE(MAX(seq), 0) FROM events")
        projects = tx.scalar("SELECT COUNT(*) FROM projects")
        leader = None
        row = tx.one("SELECT provider, model FROM leadership WHERE id = 1")
        if row is not None:
            leader = {"provider": row["provider"], "model": row["model"]}
        else:
            configured = (records.settings_all(tx).get("leadership") or {}).get("leader")
            if isinstance(configured, dict) and configured.get("provider"):
                leader = {"provider": configured["provider"], "model": configured.get("model")}
        return {
            "task_counts": counts, "pending_approvals": pending, "live_runs": live_runs,
            "last_seq": int(last_seq or 0), "projects": projects, "leader": leader,
        }

    data = await rt.ctx.db.read(fn)
    data["leader_state"] = "set" if data["leader"] is not None else ("detecting" if default_state == "detecting" else "none")
    return {
        "version": __version__,
        "uptime_seconds": {"value": round(time.monotonic() - rt.started_monotonic, 1), "exactness": "measured", "source": "process"},
        "scheduler": {
            "running": rt.scheduler.is_running,
            "assignment_paused": rt.scheduler.assignment_paused,
            "active_tasks": rt.scheduler.running_task_ids,
        },
        "providers": sorted(rt.providers),
        "recovery": rt.recovery_report,
        **data,
    }


@router.get("/settings")
async def get_settings(rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    overrides = await rt.ctx.db.read(records.settings_all)
    defaults = default_policies(rt.ctx.config_dir)
    return {"defaults": defaults, "overrides": overrides, "effective": deep_merge(defaults, overrides)}


def _validate_limits(limits: Any) -> None:
    if not isinstance(limits, dict):
        raise ValidationFailed("limits must be an object")
    for k, v in limits.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValidationFailed(f"limits.{k} must be a number")
        if v < 0 or (v == 0 and k not in _ZERO_OK_LIMITS):
            raise ValidationFailed(f"limits.{k} must be positive")


@router.put("/settings")
async def put_settings(body: SettingsUpdate, rt: Runtime = Depends(get_rt)) -> dict[str, Any]:
    if body.values.get("limits") is not None:  # null deletes the override (handled below)
        _validate_limits(body.values["limits"])

    def fn(tx: Tx) -> dict[str, Any]:
        current = records.settings_all(tx)
        for key, value in body.values.items():
            if value is None:
                records.setting_delete(tx, key)
            elif isinstance(value, dict) and isinstance(current.get(key), dict):
                records.setting_put(tx, key, deep_merge(current[key], value))
            else:
                records.setting_put(tx, key, value)
        return records.settings_all(tx)

    overrides = await rt.ctx.db.write(fn)
    policies = await rt.ctx.policies()
    rt.allowed_origins = effective_origins(policies.security.get("allowed_origins"))
    rt.scheduler.wake()
    defaults = default_policies(rt.ctx.config_dir)
    return {"defaults": defaults, "overrides": overrides, "effective": deep_merge(defaults, overrides)}
