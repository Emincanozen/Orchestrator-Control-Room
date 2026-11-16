import json

import pytest

import an_seed as S
from orchestrator.analytics import metrics
from orchestrator.analytics.metrics import Scope, percentile
from orchestrator.db import telemetry

D1 = "2026-03-01T"


@pytest.fixture
def ctx(tmp_path):
    c = S.open_ctx(tmp_path)
    yield c
    c.close()


def seed(ctx):
    """Hand-computable scenario; expected values are derived in the assertions below."""
    p = S.project(ctx, "/tmp/p")
    t1 = S.task(ctx, p, retries=1, active=100, finished=D1 + "09:10:00+00:00")
    S.run(ctx, t1, provider="claude", model="m1", status="failed", start=D1 + "09:00:00+00:00", end=D1 + "09:01:00+00:00",
          category="test_failure", usage={"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.5, "cost_basis": "list_price_equivalent"}, agent_id="ag1")
    S.run(ctx, t1, provider="codex", model="c1", phase="repair", status="succeeded", start=D1 + "09:05:00+00:00", end=D1 + "09:07:00+00:00",
          usage={"input_tokens": 200, "output_tokens": 100, "cached_input_tokens": 20})
    S.verification(ctx, t1, "failed", D1 + "09:02:00+00:00")
    S.verification(ctx, t1, "passed", D1 + "09:08:00+00:00")
    t2 = S.task(ctx, p, repairs=1, active=200, finished=D1 + "10:05:00+00:00")
    S.run(ctx, t2, provider="claude", model="m1", start=D1 + "10:00:00+00:00", end=D1 + "10:03:00+00:00",
          usage={"input_tokens": 300, "output_tokens": 150, "cost_usd": 1.0}, agent_id="ag1")
    S.verification(ctx, t2, "passed", D1 + "10:04:00+00:00")
    t3 = S.task(ctx, p, state="FAILED", retries=2, repairs=2, active=0, finished=D1 + "11:02:00+00:00")
    S.run(ctx, t3, provider="claude", model="m1", status="failed", start=D1 + "11:00:00+00:00", end=D1 + "11:00:30+00:00", category="rate_limit")
    S.verification(ctx, t3, "failed", D1 + "11:01:00+00:00")
    t4 = S.task(ctx, p, task_type="docs", active=300, finished="2026-03-05T12:11:00+00:00")
    S.run(ctx, t4, provider="claude", model="m2", start="2026-03-05T12:00:00+00:00", end="2026-03-05T12:10:00+00:00")
    S.verification(ctx, t4, "unverifiable_accepted", "2026-03-05T12:11:00+00:00")
    S.task(ctx, p, state="CREATED", finished=D1 + "09:00:00+00:00", finished_at=None)
    for i, (prov, state, t) in enumerate([("claude", "AVAILABLE", "08"), ("claude", "EXHAUSTED", "09"), ("claude", "AVAILABLE", "10"), ("codex", "LOW", "08")]):
        for m in ("requests", "tokens"):  # two metric rows per observation must count as ONE observation
            S.sql(ctx, "INSERT INTO usage_snapshots (id, provider, state, metric, exactness, source, observed_at) VALUES (?,?,?,?,?,?,?)",
                  (S.uid("snap"), prov, state, m, "estimated", "test", f"{D1}{t}:00:00+00:00"))
    for kind in ("handoff", "handoff", "auto"):
        S.sql(ctx, "INSERT INTO checkpoints (id, task_id, kind, summary, created_at) VALUES (?,?,?,?,?)", (S.uid("ckp"), t1, kind, "{}", D1 + "09:06:00+00:00"))
    S.sql(ctx, "INSERT INTO agents (id, name, role, room, activity, since, seat_order) VALUES ('ag1','Ada','worker','office','idle','2026-01-01T00:00:00+00:00',1)")
    S.sql(ctx, "INSERT INTO agents (id, name, role, room, activity, since, seat_order) VALUES ('ag2','Bo','worker','office','idle','2026-01-01T00:00:00+00:00',2)")
    return p


def m(summary, key):
    return summary["metrics"][key]


def test_percentile_linear():
    assert percentile([30, 60, 120, 180, 600], 0.5) == 120
    assert percentile([30, 60, 120, 180, 600], 0.9) == pytest.approx(432)
    assert percentile([], 0.5) is None


def test_empty_db_everything_missing(ctx):
    s = ctx.db.read_sync(lambda tx: metrics.summary(tx, Scope.make()))
    assert s["metrics"], "metrics are present"
    for k, v in s["metrics"].items():
        assert v["exactness"] == "missing" and v["value"] is None and v["n"] == 0, k
        assert set(v) == {"value", "exactness", "source", "n"}
    assert ctx.db.read_sync(lambda tx: metrics.agents(tx, Scope.make())) == []
    ts = ctx.db.read_sync(lambda tx: metrics.timeseries(tx, Scope.make(), "tasks_completed"))
    assert ts["exactness"] == "missing" and ts["points"] == []


def test_summary_hand_computed(ctx):
    seed(ctx)
    s = ctx.db.read_sync(lambda tx: metrics.summary(tx, Scope.make()))
    assert m(s, "tasks_completed") == {"value": 3, "exactness": "measured", "source": "tasks.state = COMPLETED", "n": 5}
    r = m(s, "verified_completion_rate")  # T1,T2 verified of T1,T2,T3,T4 terminal/failed
    assert (r["value"], r["n"], r["exactness"]) == (0.5, 4, "measured")
    assert (m(s, "test_pass_rate")["value"], m(s, "test_pass_rate")["n"]) == (0.4, 5)  # 2 passed / 5 verifications
    assert m(s, "retries_total")["value"] == 3 and m(s, "retries_total")["n"] == 5
    assert m(s, "retries_per_task")["value"] == 0.6
    assert m(s, "repair_cycles_total")["value"] == 3
    assert m(s, "failure_frequency")["value"] == {"rate_limit": 1, "test_failure": 1} and m(s, "failure_frequency")["n"] == 2
    d = m(s, "run_duration_seconds")  # 30,60,120,180,600
    assert d["value"] == {"p50": 120.0, "p90": 432.0, "mean": 198.0} and d["n"] == 5
    a = m(s, "task_active_seconds")  # 100,200,300 (T3 has 0 -> excluded)
    assert a["value"] == {"p50": 200.0, "p90": 280.0, "mean": 200.0} and a["n"] == 3
    sw = m(s, "provider_model_switches")  # only T1 switched (claude/m1 -> codex/c1) among 4 tasks with runs
    assert sw["value"] == {"total": 1, "per_task_avg": 0.25, "tasks_with_switch": 1} and sw["n"] == 4
    tok = m(s, "tokens")
    assert (tok["value"]["input"], tok["value"]["output"], tok["value"]["cached"], tok["n"]) == (600, 300, 20, 3)
    assert tok["value"]["by_model"]["claude/m1"] == {"input": 400, "output": 200, "cached": None, "runs": 2}
    assert tok["exactness"] == "measured"
    cost = m(s, "cost_usd_estimated")
    assert (cost["value"], cost["exactness"], cost["n"]) == (1.5, "estimated", 2)
    av = m(s, "provider_availability")
    assert av["value"]["claude"]["available_share"] == pytest.approx(2 / 3, abs=1e-4) and av["value"]["claude"]["observations"] == 3
    assert av["value"]["codex"]["available_share"] == 1.0 and av["exactness"] == "estimated" and av["n"] == 4
    assert m(s, "handoff_checkpoints")["value"] == 2
    assert m(s, "handoff_prompt_chars")["exactness"] == "missing"


def test_filters_provider_and_dates(ctx):
    seed(ctx)
    cod = ctx.db.read_sync(lambda tx: metrics.summary(tx, Scope.make(provider="codex")))
    assert m(cod, "tasks_completed")["value"] == 1 and m(cod, "tasks_completed")["n"] == 1  # only T1 has a codex run
    assert m(cod, "run_duration_seconds")["value"]["mean"] == 120.0 and m(cod, "run_duration_seconds")["n"] == 1
    early = ctx.db.read_sync(lambda tx: metrics.summary(tx, Scope.make(to="2026-03-01")))  # date-only 'to' covers that whole day
    assert m(early, "tasks_completed")["value"] == 2 and m(early, "tasks_completed")["n"] == 4
    assert m(early, "run_duration_seconds")["value"]["p50"] == 90.0 and m(early, "run_duration_seconds")["n"] == 4
    docs = ctx.db.read_sync(lambda tx: metrics.summary(tx, Scope.make(task_type="docs")))
    assert m(docs, "tasks_completed")["n"] == 1
    with pytest.raises(ValueError):
        Scope.make(from_="not-a-date")


def test_small_n_is_reported(ctx):
    p = S.project(ctx, "/tmp/p")
    t = S.task(ctx, p)
    S.verification(ctx, t, "passed", D1 + "09:30:00+00:00")
    s = ctx.db.read_sync(lambda tx: metrics.summary(tx, Scope.make()))
    assert m(s, "test_pass_rate")["value"] == 1.0 and m(s, "test_pass_rate")["n"] == 1
    assert m(s, "verified_completion_rate")["n"] == 1
    assert m(s, "run_duration_seconds")["exactness"] == "missing"  # no runs: not a fabricated 0


def test_timeseries(ctx):
    seed(ctx)
    ts = ctx.db.read_sync(lambda tx: metrics.timeseries(tx, Scope.make(), "tasks_completed"))
    assert [(p["bucket"], p["value"]) for p in ts["points"]] == [("2026-03-01", 2), ("2026-03-05", 1)]
    tp = ctx.db.read_sync(lambda tx: metrics.timeseries(tx, Scope.make(), "test_pass_rate"))
    assert [(p["bucket"], p["value"], p["n"]) for p in tp["points"]] == [("2026-03-01", 0.5, 4), ("2026-03-05", 0.0, 1)]
    ti = ctx.db.read_sync(lambda tx: metrics.timeseries(tx, Scope.make(), "tokens_input"))
    assert [(p["bucket"], p["value"]) for p in ti["points"]] == [("2026-03-01", 600)]
    cs = ctx.db.read_sync(lambda tx: metrics.timeseries(tx, Scope.make(), "cost_usd_estimated"))
    assert cs["exactness"] == "estimated" and cs["points"][0]["value"] == 1.5
    with pytest.raises(ValueError):
        ctx.db.read_sync(lambda tx: metrics.timeseries(tx, Scope.make(), "bogus"))


def test_agents(ctx):
    seed(ctx)
    rows = {a["agent_id"]: a for a in ctx.db.read_sync(lambda tx: metrics.agents(tx, Scope.make()))}
    a1 = rows["ag1"]
    assert a1["runs"]["value"] == 2 and a1["succeeded"]["value"] == 1 and a1["failed"]["value"] == 1
    assert a1["working_seconds"]["value"] == 240.0 and a1["working_seconds"]["n"] == 2
    assert rows["ag2"]["runs"]["exactness"] == "missing" and rows["ag2"]["working_seconds"]["value"] is None


def test_model_table_matches_history_stats(ctx):
    seed(ctx)
    table = ctx.db.read_sync(lambda tx: metrics.model_table(tx))
    hist = ctx.db.read_sync(telemetry.history_stats)
    by = {(r["provider"], r["model"], r["task_type"]): r for r in table}
    for key, h in hist.items():  # the single shared definition
        assert (by[key]["verified_runs"], by[key]["successes"]) == (h["verified_runs"], h["successes"])
    c = by[("claude", "m1", "coding")]
    assert (c["runs"], c["verified_runs"], c["successes"]) == (3, 2, 1)
    assert c["success_rate"]["value"] == 0.5 and c["success_rate"]["n"] == 2
    assert c["avg_duration_seconds"]["value"] == 90.0 and c["p50_duration_seconds"]["value"] == 60.0
    assert c["avg_tokens"]["value"] == 300.0 and c["last_used"] == D1 + "11:00:00+00:00"
    x = by[("codex", "c1", "coding")]
    assert (x["verified_runs"], x["successes"], x["success_rate"]["value"]) == (1, 1, 1.0)
    d = by[("claude", "m2", "docs")]  # unverifiable_accepted is not a 'passed' verification
    assert d["verified_runs"] == 0 and d["success_rate"]["exactness"] == "missing" and d["success_rate"]["value"] is None
    only = ctx.db.read_sync(lambda tx: metrics.model_table(tx, provider="codex"))
    assert [r["model"] for r in only] == ["c1"]
    json.dumps(table)


# --------------------------------------------------------------------------- M9a
def test_provider_availability_excludes_unknown_observations_from_the_share(ctx):
    seed(ctx)
    av = m(ctx.db.read_sync(lambda tx: metrics.summary(tx, Scope.make())), "provider_availability")
    for name in ("claude", "codex"):  # fully known seed: shape is a superset of the old one
        v = av["value"][name]
        assert v["unknown_observations"] == 0 and v["known_observations"] == v["observations"] and v["exactness"] == "estimated"


def _snap(ctx, provider, state, at, metric_name="x"):
    def fn(tx):
        telemetry.snapshot_insert(tx, {
            "id": f"usg_{provider}_{at}_{metric_name}", "provider": provider, "model": None, "state": state, "metric": metric_name,
            "value": None, "unit": None, "exactness": "exact", "source": "test", "confidence": 0.9, "resets_at": None, "observed_at": at})

    ctx.db.write_sync(fn)


def test_provider_availability_unknown_is_reported_separately_and_never_zero(ctx):
    for i in range(30):  # a provider that exposes no quota data: only UNKNOWN observations
        _snap(ctx, "agy", "UNKNOWN", f"2026-03-01T10:{i:02d}:00+00:00", "input_tokens")
    for i, st in enumerate(["AVAILABLE", "EXHAUSTED", "UNKNOWN", "LOW"]):
        _snap(ctx, "claude", st, f"2026-03-01T11:0{i}:00+00:00", "rate_limit_status")
    av = m(ctx.db.read_sync(lambda tx: metrics.summary(tx, Scope.make())), "provider_availability")
    assert av["exactness"] == "estimated" and av["n"] == 3  # known observations only
    agy, claude = av["value"]["agy"], av["value"]["claude"]
    assert agy["available_share"] is None and agy["known_observations"] == 0 and agy["unknown_observations"] == 30 and agy["exactness"] == "missing"
    assert claude["available_share"] == pytest.approx(2 / 3, abs=1e-4) and claude["known_observations"] == 3 and claude["unknown_observations"] == 1
    assert claude["last_observed_at"] == "2026-03-01T11:03:00+00:00"


def test_provider_availability_missing_when_nothing_is_known(ctx):
    _snap(ctx, "agy", "UNKNOWN", "2026-03-01T10:00:00+00:00")
    av = m(ctx.db.read_sync(lambda tx: metrics.summary(tx, Scope.make())), "provider_availability")
    assert av["exactness"] == "missing" and av["n"] == 0 and av["value"]["agy"]["available_share"] is None


def test_handoff_prompt_chars_is_measured_from_runs(ctx):
    p = S.project(ctx, "/tmp/p")
    t = S.task(ctx, p)
    S.run(ctx, t, usage={"prompt_chars": 1000, "handoff_chars": 400, "knowledge_chars": 100})
    S.run(ctx, t, phase="repair", usage={"prompt_chars": 3000, "handoff_chars": 600, "knowledge_chars": 0})
    S.run(ctx, t, phase="plan", usage={"prompt_chars": 50})  # leader prompts carry no handoff: not counted
    h = m(ctx.db.read_sync(lambda tx: metrics.summary(tx, Scope.make())), "handoff_prompt_chars")
    assert h["exactness"] == "measured" and h["n"] == 2
    assert h["value"]["handoff_chars"]["mean"] == 500.0 and h["value"]["prompt_chars"]["max"] == 3000.0
    assert h["value"]["knowledge_chars"]["mean"] == 50.0
