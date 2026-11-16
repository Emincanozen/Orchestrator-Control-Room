"""Analytics queries over real rows. Every value is a metric object:

    {"value": <json | null>, "exactness": "measured"|"estimated"|"ai_evaluated"|"missing", "source": str, "n": int}

A metric with no underlying rows is `missing` with value null: never 0 pretending to be data, never fabricated.
Counts are `measured` 0 only when the scope has rows to count from (e.g. 0 completed among 3 finished tasks).
Read-only: no function here writes to the database.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional, Sequence

from orchestrator.db import telemetry
from orchestrator.db.database import Tx
from orchestrator.util import parse_iso, to_iso

AVAILABLE_STATES = ("AVAILABLE", "LOW")
TIMESERIES_METRICS = ("tasks_completed", "runs_started", "runs_failed", "test_pass_rate", "tokens_input", "tokens_output",
                      "cost_usd_estimated")


def metric(value: Any, exactness: str, source: str, n: int) -> dict[str, Any]:
    return {"value": value, "exactness": exactness, "source": source, "n": int(n)}


def missing(source: str, note: str = "") -> dict[str, Any]:
    return {"value": None, "exactness": "missing", "source": source + (f" ({note})" if note else ""), "n": 0}


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear interpolation between closest ranks (q in 0..1)."""
    if not values:
        return None
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _r(x: Optional[float], nd: int = 3) -> Optional[float]:
    return None if x is None else round(x, nd)


@dataclass(frozen=True)
class Scope:
    project_id: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    task_type: Optional[str] = None
    from_: Optional[str] = None  # inclusive ISO instant
    to: Optional[str] = None  # exclusive ISO instant

    @staticmethod
    def make(project_id: Optional[str] = None, provider: Optional[str] = None, model: Optional[str] = None,
             task_type: Optional[str] = None, from_: Optional[str] = None, to: Optional[str] = None) -> "Scope":
        return Scope(project_id or None, provider or None, model or None, task_type or None, _bound(from_, False), _bound(to, True))

    def echo(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "provider": self.provider, "model": self.model, "task_type": self.task_type,
                "from": self.from_, "to": self.to}

    # -- SQL fragments (aliases: t = tasks, r = runs, v = verifications) --------------------------
    def _time(self, col: str) -> tuple[str, list[Any]]:
        sql, p = "", []
        if self.from_:
            sql += f" AND {col} >= ?"
            p.append(self.from_)
        if self.to:
            sql += f" AND {col} < ?"
            p.append(self.to)
        return sql, p

    def _task_filters(self) -> tuple[str, list[Any]]:
        sql, p = "", []
        if self.project_id:
            sql += " AND t.project_id = ?"
            p.append(self.project_id)
        if self.task_type:
            sql += " AND t.task_type = ?"
            p.append(self.task_type)
        if self.provider or self.model:
            sql += " AND EXISTS (SELECT 1 FROM runs rr WHERE rr.task_id = t.id" + (" AND rr.provider = ?" if self.provider else "") + (
                " AND rr.model = ?" if self.model else "") + ")"
            p += [x for x in (self.provider, self.model) if x]
        return sql, p

    def tasks(self, time_col: str = "COALESCE(t.finished_at, t.updated_at)") -> tuple[str, list[Any]]:
        a, pa = self._task_filters()
        b, pb = self._time(time_col)
        return a + b, pa + pb

    def runs(self) -> tuple[str, list[Any]]:
        """Use with: FROM runs r LEFT JOIN tasks t ON t.id = r.task_id WHERE 1=1 {sql}"""
        sql, p = "", []
        if self.project_id:
            sql += " AND t.project_id = ?"
            p.append(self.project_id)
        if self.task_type:
            sql += " AND t.task_type = ?"
            p.append(self.task_type)
        if self.provider:
            sql += " AND r.provider = ?"
            p.append(self.provider)
        if self.model:
            sql += " AND r.model = ?"
            p.append(self.model)
        b, pb = self._time("r.started_at")
        return sql + b, p + pb

    def verifications(self) -> tuple[str, list[Any]]:
        """Use with: FROM verifications v JOIN tasks t ON t.id = v.task_id WHERE 1=1 {sql}"""
        a, pa = self._task_filters()
        b, pb = self._time("v.started_at")
        return a + b, pa + pb


def _bound(value: Optional[str], is_to: bool) -> Optional[str]:
    if not value:
        return None
    dt = parse_iso(value)
    if dt is None:
        raise ValueError(f"invalid timestamp {value!r}")
    if is_to:  # a date-only upper bound covers that whole day; an instant is inclusive
        dt = dt + (timedelta(days=1) if len(value) == 10 else timedelta(microseconds=1))
    return to_iso(dt)


def _num(path: str) -> str:
    x = f"json_extract(r.usage, '$.{path}')"
    return f"CASE WHEN json_valid(r.usage) AND typeof({x}) IN ('integer','real') THEN {x} END"


# ---- summary ------------------------------------------------------------------------------------


def summary(tx: Tx, sc: Scope) -> dict[str, Any]:
    out: dict[str, Any] = {}
    tw, tp = sc.tasks()
    tasks = tx.all(
        "SELECT t.id, t.state, t.retry_count, t.repair_cycles, t.active_seconds, "
        "EXISTS (SELECT 1 FROM verifications v WHERE v.task_id = t.id AND v.status = 'passed') AS has_pass "
        f"FROM tasks t WHERE 1=1{tw}", tp)
    n_tasks = len(tasks)
    src_t = "tasks.state (+ verifications)"
    if n_tasks == 0:
        for k in ("tasks_completed", "verified_completion_rate", "retries_total", "retries_per_task", "repair_cycles_total", "task_active_seconds"):
            out[k] = missing(src_t, "no tasks in scope")
    else:
        completed = [t for t in tasks if t["state"] == "COMPLETED"]
        out["tasks_completed"] = metric(len(completed), "measured", "tasks.state = COMPLETED", n_tasks)
        done = [t for t in tasks if t["state"] in ("COMPLETED", "FAILED", "CANCELLED")]
        if done:
            ok = sum(1 for t in completed if t["has_pass"])
            out["verified_completion_rate"] = metric(
                _r(ok / len(done), 4), "measured",
                "COMPLETED tasks with a 'passed' verification / tasks in COMPLETED, FAILED or CANCELLED", len(done))
        else:
            out["verified_completion_rate"] = missing(src_t, "no task reached a terminal or FAILED state")
        rt_ = sum(t["retry_count"] for t in tasks)
        out["retries_total"] = metric(rt_, "measured", "SUM(tasks.retry_count)", n_tasks)
        out["retries_per_task"] = metric(_r(rt_ / n_tasks, 4), "measured", "SUM(tasks.retry_count) / tasks", n_tasks)
        out["repair_cycles_total"] = metric(sum(t["repair_cycles"] for t in tasks), "measured", "SUM(tasks.repair_cycles)", n_tasks)
        act = [float(t["active_seconds"]) for t in tasks if t["active_seconds"] and t["active_seconds"] > 0]
        out["task_active_seconds"] = (
            metric({"p50": _r(percentile(act, .5)), "p90": _r(percentile(act, .9)), "mean": _r(sum(act) / len(act))}, "measured",
                   "tasks.active_seconds (> 0)", len(act)) if act else missing("tasks.active_seconds", "no task has active time"))

    # test pass rate
    vw, vp = sc.verifications()
    v = tx.one(f"SELECT COUNT(*) AS n, SUM(CASE WHEN v.status = 'passed' THEN 1 ELSE 0 END) AS ok FROM verifications v JOIN tasks t ON t.id = v.task_id WHERE 1=1{vw}", vp)
    out["test_pass_rate"] = (metric(_r(v["ok"] / v["n"], 4), "measured", "verifications.status = 'passed' / all verifications", v["n"])
                             if v["n"] else missing("verifications"))

    # runs
    rw, rp = sc.runs()
    base = f"FROM runs r LEFT JOIN tasks t ON t.id = r.task_id WHERE 1=1{rw}"
    n_runs = tx.scalar(f"SELECT COUNT(*) {base}", rp)
    if not n_runs:
        for k in ("failure_frequency", "run_duration_seconds"):
            out[k] = missing("runs", "no runs in scope")
    else:
        fails = tx.all(f"SELECT COALESCE(r.error_category, 'unknown') AS cat, COUNT(*) AS n {base} AND r.status = 'failed' GROUP BY cat ORDER BY n DESC, cat", rp)
        out["failure_frequency"] = metric({r["cat"]: r["n"] for r in fails}, "measured", "runs.error_category of failed runs",
                                          sum(r["n"] for r in fails))
        durs = [float(r[0]) for r in tx.all(
            f"SELECT (julianday(r.finished_at) - julianday(r.started_at)) * 86400.0 {base} AND r.finished_at IS NOT NULL", rp) if r[0] is not None]
        out["run_duration_seconds"] = (
            metric({"p50": _r(percentile(durs, .5)), "p90": _r(percentile(durs, .9)), "mean": _r(sum(durs) / len(durs))}, "measured",
                   "runs.finished_at - runs.started_at", len(durs)) if durs else missing("runs", "no finished runs"))

    # provider / model switches per task (consecutive execute/repair runs of one task that differ)
    sw = tx.all(
        "SELECT task_id, SUM(CASE WHEN prev IS NOT NULL AND prev <> cur THEN 1 ELSE 0 END) AS sw FROM ("
        " SELECT r.task_id AS task_id, r.provider || '/' || COALESCE(r.model, '') AS cur,"
        " LAG(r.provider || '/' || COALESCE(r.model, '')) OVER (PARTITION BY r.task_id ORDER BY r.started_at, r.rowid) AS prev"
        f" {base} AND r.phase IN ('execute','repair') AND r.task_id IS NOT NULL) GROUP BY task_id", rp)
    out["provider_model_switches"] = (
        metric({"total": sum(r["sw"] for r in sw), "per_task_avg": _r(sum(r["sw"] for r in sw) / len(sw), 4),
                "tasks_with_switch": sum(1 for r in sw if r["sw"])}, "measured",
               "consecutive execute/repair runs of a task with a different provider/model", len(sw))
        if sw else missing("runs", "no execute/repair runs"))

    # tokens and cost (provider-reported; absent for providers that do not report)
    tok = tx.all(
        f"SELECT r.provider AS provider, COALESCE(r.model, '') AS model, COUNT(*) AS runs, SUM({_num('input_tokens')}) AS i, "
        f"SUM({_num('output_tokens')}) AS o, SUM({_num('cached_input_tokens')}) AS c {base} "
        f"AND ({_num('input_tokens')} IS NOT NULL OR {_num('output_tokens')} IS NOT NULL) GROUP BY r.provider, COALESCE(r.model, '') ORDER BY 1, 2", rp)
    if tok:
        by = {f"{r['provider']}/{r['model']}" if r["model"] else r["provider"]: {
            "input": int(r["i"] or 0), "output": int(r["o"] or 0), "cached": int(r["c"] or 0) if r["c"] is not None else None,
            "runs": r["runs"]} for r in tok}
        out["tokens"] = metric({"input": sum(b["input"] for b in by.values()), "output": sum(b["output"] for b in by.values()),
                                "cached": sum(b["cached"] or 0 for b in by.values()), "by_model": by}, "measured",
                               "runs.usage token counts as reported by the provider (runs without usage are not counted)",
                               sum(b["runs"] for b in by.values()))
    else:
        out["tokens"] = missing("runs.usage", "no run reported token usage")
    cost = tx.one(f"SELECT COUNT(*) AS n, SUM({_num('cost_usd')}) AS usd {base} AND r.provider = 'claude' AND {_num('cost_usd')} IS NOT NULL", rp)
    out["cost_usd_estimated"] = (
        metric(round(cost["usd"], 6), "estimated", "claude runs.usage.cost_usd: list-price equivalent, NOT money charged (subscription CLI)", cost["n"])
        if cost["n"] else missing("runs.usage.cost_usd", "no Claude run reported a cost"))

    # provider availability from usage state history
    av_sql, av_p = "", []
    if sc.provider:
        av_sql += " AND provider = ?"
        av_p.append(sc.provider)
    tm, tmp = sc._time("observed_at")
    # An observation (provider, instant) is "unknown" when every snapshot in it is UNKNOWN (token counts, cost, providers
    # that expose no quota data). Unknown observations are NOT counted as unavailable: they are reported separately and
    # excluded from the share's denominator.
    av = tx.all(
        "SELECT provider, COUNT(*) AS n, SUM(CASE WHEN known > 0 THEN 1 ELSE 0 END) AS known,"
        " SUM(CASE WHEN known = 0 THEN 1 ELSE 0 END) AS unknown, SUM(CASE WHEN known > 0 THEN ok ELSE 0 END) AS ok,"
        " MAX(observed_at) AS last FROM ("
        " SELECT provider, observed_at, SUM(CASE WHEN state <> 'UNKNOWN' THEN 1 ELSE 0 END) AS known,"
        " MIN(CASE WHEN state = 'UNKNOWN' THEN NULL WHEN state IN ('AVAILABLE','LOW') THEN 1 ELSE 0 END) AS ok"
        f" FROM usage_snapshots WHERE 1=1{av_sql}{tm} GROUP BY provider, observed_at) GROUP BY provider ORDER BY provider", av_p + tmp)
    if av:
        per = {
            r["provider"]: {
                "available_share": _r(r["ok"] / r["known"], 4) if r["known"] else None,
                "known_observations": r["known"], "unknown_observations": r["unknown"],
                "observations": r["n"],  # known + unknown (kept for older clients)
                "last_observed_at": r["last"],
                "exactness": "estimated" if r["known"] else "missing",
            } for r in av
        }
        known_total = sum(r["known"] for r in av)
        out["provider_availability"] = (
            metric(per, "estimated", "share of KNOWN usage_snapshots observations in state AVAILABLE/LOW (sampled, not time-weighted); "
                   "UNKNOWN observations are excluded and counted separately", known_total)
            if known_total else {"value": per, "exactness": "missing",
                                 "source": "usage_snapshots (only UNKNOWN observations: no provider exposed quota data)", "n": 0})
    else:
        out["provider_availability"] = missing("usage_snapshots", "no observations")

    # context-transfer overhead
    cw, cp = "", []
    if sc.project_id:
        cw += " AND t.project_id = ?"
        cp.append(sc.project_id)
    if sc.task_type:
        cw += " AND t.task_type = ?"
        cp.append(sc.task_type)
    ctm, ctp = sc._time("c.created_at")
    ho = tx.scalar(f"SELECT COUNT(*) FROM checkpoints c JOIN tasks t ON t.id = c.task_id WHERE c.kind = 'handoff'{cw}{ctm}", cp + ctp)
    out["handoff_checkpoints"] = (metric(ho, "measured", "checkpoints.kind = 'handoff'", ho) if ho else missing("checkpoints", "no handoff checkpoint"))
    sizes = tx.all(
        f"SELECT {_num('prompt_chars')} AS p, {_num('handoff_chars')} AS h, {_num('knowledge_chars')} AS k {base} "
        f"AND {_num('handoff_chars')} IS NOT NULL", rp)

    def dist(key: str) -> Optional[dict[str, Any]]:
        vals = [float(r[key]) for r in sizes if r[key] is not None]
        return ({"p50": _r(percentile(vals, .5)), "p90": _r(percentile(vals, .9)), "mean": _r(sum(vals) / len(vals)), "max": max(vals)}
                if vals else None)

    out["handoff_prompt_chars"] = (
        metric({"prompt_chars": dist("p"), "handoff_chars": dist("h"), "knowledge_chars": dist("k")}, "measured",
               "runs.usage.prompt_chars / handoff_chars / knowledge_chars recorded by the executor for execute/repair runs", len(sizes))
        if sizes else missing("runs.usage.handoff_chars", "no execute/repair run recorded its prompt size"))
    return {"scope": sc.echo(), "metrics": out}


# ---- time series --------------------------------------------------------------------------------


def timeseries(tx: Tx, sc: Scope, name: str) -> dict[str, Any]:
    if name not in TIMESERIES_METRICS:
        raise ValueError(f"unknown metric {name!r}; one of {', '.join(TIMESERIES_METRICS)}")
    day = lambda col: f"substr({col}, 1, 10)"  # noqa: E731  (UTC ISO timestamps)
    rw, rp = sc.runs()
    rbase = f"FROM runs r LEFT JOIN tasks t ON t.id = r.task_id WHERE 1=1{rw}"
    kind, src = "count", ""
    if name == "tasks_completed":
        tw, tp = sc.tasks("t.finished_at")
        rows = tx.all(f"SELECT {day('t.finished_at')} AS b, COUNT(*) AS v, COUNT(*) AS n FROM tasks t WHERE t.state = 'COMPLETED' AND t.finished_at IS NOT NULL{tw} GROUP BY b ORDER BY b", tp)
        src = "tasks.finished_at of COMPLETED tasks"
    elif name == "runs_started":
        rows = tx.all(f"SELECT {day('r.started_at')} AS b, COUNT(*) AS v, COUNT(*) AS n {rbase} GROUP BY b ORDER BY b", rp)
        src = "runs.started_at"
    elif name == "runs_failed":
        rows = tx.all(f"SELECT {day('r.started_at')} AS b, COUNT(*) AS v, COUNT(*) AS n {rbase} AND r.status = 'failed' GROUP BY b ORDER BY b", rp)
        src = "runs.started_at of failed runs"
    elif name == "test_pass_rate":
        vw, vp = sc.verifications()
        rows = tx.all(f"SELECT {day('v.started_at')} AS b, ROUND(1.0 * SUM(v.status = 'passed') / COUNT(*), 4) AS v, COUNT(*) AS n "
                      f"FROM verifications v JOIN tasks t ON t.id = v.task_id WHERE 1=1{vw} GROUP BY b ORDER BY b", vp)
        kind, src = "rate", "verifications.started_at"
    elif name in ("tokens_input", "tokens_output"):
        f = _num("input_tokens" if name == "tokens_input" else "output_tokens")
        rows = tx.all(f"SELECT {day('r.started_at')} AS b, SUM({f}) AS v, COUNT({f}) AS n {rbase} AND {f} IS NOT NULL GROUP BY b ORDER BY b", rp)
        src = "runs.usage (provider-reported)"
    else:
        f = _num("cost_usd")
        rows = tx.all(f"SELECT {day('r.started_at')} AS b, ROUND(SUM({f}), 6) AS v, COUNT({f}) AS n {rbase} AND r.provider = 'claude' AND {f} IS NOT NULL GROUP BY b ORDER BY b", rp)
        src = "claude runs.usage.cost_usd (list-price equivalent)"
    points = [{"bucket": r["b"], "value": r["v"], "n": r["n"]} for r in rows]  # days without data are omitted, not zero-filled
    exactness = "missing" if not points else ("estimated" if name == "cost_usd_estimated" else "measured")
    return {"metric": name, "bucket": "day", "scope": sc.echo(), "exactness": exactness, "source": src, "points": points,
            "n": sum(p["n"] for p in points)}


# ---- agents --------------------------------------------------------------------------------------


def agents(tx: Tx, sc: Scope) -> list[dict[str, Any]]:
    rw, rp = sc.runs()
    stats = {r["agent_id"]: r for r in tx.all(
        "SELECT r.agent_id AS agent_id, COUNT(*) AS runs, SUM(r.status = 'succeeded') AS ok, SUM(r.status = 'failed') AS bad, MAX(r.started_at) AS last, "
        "SUM(CASE WHEN r.finished_at IS NOT NULL THEN (julianday(r.finished_at) - julianday(r.started_at)) * 86400.0 END) AS secs, "
        f"SUM(r.finished_at IS NOT NULL) AS fin FROM runs r LEFT JOIN tasks t ON t.id = r.task_id WHERE r.agent_id IS NOT NULL{rw} GROUP BY r.agent_id", rp)}
    out = []
    for a in tx.all("SELECT id, name, role, room, provider, model, activity FROM agents ORDER BY seat_order, id"):
        s = stats.get(a["id"])
        row = {"agent_id": a["id"], "name": a["name"], "role": a["role"], "room": a["room"], "provider": a["provider"], "model": a["model"],
               "activity": a["activity"]}
        if s is None:
            row.update(runs=missing("runs.agent_id", "no runs for this seat"), working_seconds=missing("runs", "no runs for this seat"),
                       succeeded=missing("runs.agent_id"), failed=missing("runs.agent_id"), last_run_at=None)
        else:
            row.update(runs=metric(s["runs"], "measured", "COUNT(runs by agent_id)", s["runs"]),
                       succeeded=metric(s["ok"], "measured", "runs.status = succeeded", s["runs"]),
                       failed=metric(s["bad"], "measured", "runs.status = failed", s["runs"]),
                       working_seconds=(metric(_r(s["secs"]), "measured", "SUM(finished_at - started_at) of finished runs", s["fin"])
                                        if s["fin"] else missing("runs", "no finished runs")),
                       last_run_at=s["last"])
        out.append(row)
    return out


# ---- model performance ------------------------------------------------------------------------------


def model_table(tx: Tx, *, provider: Optional[str] = None, model: Optional[str] = None, task_type: Optional[str] = None) -> list[dict[str, Any]]:
    """One row per (provider, model, task_type). verified_runs / successes come from db.telemetry.history_stats
    (the ONE definition used by adaptive routing); runs / durations / tokens are plain aggregates of the same population
    (execute/repair runs with a model)."""
    hist = telemetry.history_stats(tx)
    rows = tx.all(
        "SELECT r.provider AS provider, r.model AS model, t.task_type AS task_type, r.started_at AS last_used, "
        "(julianday(r.finished_at) - julianday(r.started_at)) * 86400.0 AS dur, "
        f"{_num('input_tokens')} AS ti, {_num('output_tokens')} AS to_ "
        "FROM runs r JOIN tasks t ON t.id = r.task_id WHERE r.phase IN ('execute','repair') AND r.model IS NOT NULL")
    cells: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        c = cells.setdefault((r["provider"], r["model"], r["task_type"]), {"runs": 0, "last": None, "durs": [], "toks": []})
        c["runs"] += 1
        c["last"] = max(filter(None, [c["last"], r["last_used"]]))
        if r["dur"] is not None:
            c["durs"].append(float(r["dur"]))
        if r["ti"] is not None or r["to_"] is not None:
            c["toks"].append(float((r["ti"] or 0) + (r["to_"] or 0)))
    out = []
    for key in sorted(set(cells) | set(hist)):
        p, m, t = key
        if (provider and p != provider) or (model and m != model) or (task_type and t != task_type):
            continue
        c = cells.get(key, {"runs": 0, "last": None, "durs": [], "toks": []})
        h = hist.get(key, {"verified_runs": 0, "successes": 0})
        vr, ok = h["verified_runs"], h["successes"]
        out.append({
            "provider": p, "model": m, "task_type": t, "runs": c["runs"], "verified_runs": vr, "successes": ok,
            "success_rate": (metric(_r(ok / vr, 4), "measured", "db.telemetry.history_stats: successes / verified_runs", vr)
                             if vr else missing("db.telemetry.history_stats", "no verified runs")),
            "avg_duration_seconds": (metric(_r(sum(c["durs"]) / len(c["durs"])), "measured", "runs.finished_at - started_at", len(c["durs"]))
                                     if c["durs"] else missing("runs", "no finished runs")),
            "p50_duration_seconds": (metric(_r(percentile(c["durs"], .5)), "measured", "runs.finished_at - started_at", len(c["durs"]))
                                     if c["durs"] else missing("runs", "no finished runs")),
            "avg_tokens": (metric(_r(sum(c["toks"]) / len(c["toks"]), 1), "measured", "runs.usage input+output tokens (provider-reported)", len(c["toks"]))
                           if c["toks"] else missing("runs.usage", "no token usage reported")),
            "last_used": c["last"],
        })
    return out
