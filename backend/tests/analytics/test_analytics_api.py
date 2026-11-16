import json

import pytest
from fastapi.testclient import TestClient

import an_seed as S
import orchestrator.analytics as analytics_pkg
from orchestrator.analytics import adaptive
from orchestrator.api.app import create_app
from orchestrator.api.routes import analytics as analytics_routes
from orchestrator.config import load_yaml
from orchestrator.db import records, telemetry

BASE = "http://127.0.0.1:8765"
T = "2026-03-01T"


@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=tmp_path / "data", providers={}, start_scheduler=False,
                     extra_routers=[analytics_routes.router], on_startup=[analytics_pkg.startup])
    with TestClient(app, base_url=BASE) as c:
        c.ctx = app.state.rt.ctx
        c.headers.update({"Authorization": f"Bearer {app.state.rt.token}"})
        c.app_ = app
        yield c


def seed_adaptive(ctx, verified=10, ok=8):
    p = S.project(ctx, "/tmp/p")
    t = S.task(ctx, p)
    for i in range(verified):
        S.run(ctx, t, provider="claude", model="m1", status="succeeded" if i < ok else "failed",
              start=f"{T}09:{i:02d}:00+00:00", end=f"{T}09:{i:02d}:30+00:00")
    S.verification(ctx, t, "passed", f"{T}10:00:00+00:00")
    return t


def test_auth_required(client):
    anon = TestClient(client.app_, base_url=BASE)
    for path in ("/summary", "/models", "/routing", "/timeseries?metric=runs_started", "/agents", "/adaptive-settings"):
        assert anon.get("/api/analytics" + path).status_code == 401, path
    assert anon.put("/api/analytics/adaptive-settings", json={"enabled": False}).status_code == 401


def test_api_shapes_on_empty_db(client):
    s = client.get("/api/analytics/summary").json()
    assert set(s) == {"scope", "metrics"} and s["scope"]["project_id"] is None
    assert all(v["exactness"] == "missing" for v in s["metrics"].values())
    assert client.get("/api/analytics/models").json()["models"] == []
    assert client.get("/api/analytics/agents").json()["agents"][0]["runs"]["exactness"] == "missing"  # seeded seats, no runs
    r = client.get("/api/analytics/routing").json()
    assert set(r) == {"settings", "cells", "applied_history", "decisions_with_adaptive"} and r["cells"] == []
    ts = client.get("/api/analytics/timeseries", params={"metric": "tasks_completed", "bucket": "day"}).json()
    assert set(ts) == {"metric", "bucket", "scope", "exactness", "source", "points", "n"}


def test_api_validation(client):
    assert client.get("/api/analytics/timeseries", params={"metric": "nope"}).status_code == 422
    assert client.get("/api/analytics/timeseries").status_code == 422
    assert client.get("/api/analytics/summary", params={"from": "garbage"}).status_code == 422
    assert client.get("/api/analytics/timeseries", params={"metric": "runs_started", "bucket": "week"}).status_code == 422


def test_summary_with_filters_over_api(client):
    p = S.project(client.ctx, "/tmp/p")
    t = S.task(client.ctx, p)
    S.verification(client.ctx, t, "passed", f"{T}09:00:00+00:00")
    j = client.get("/api/analytics/summary", params={"project_id": p, "from": "2026-03-01", "to": "2026-03-01"}).json()
    assert j["metrics"]["tasks_completed"]["value"] == 1
    other = client.get("/api/analytics/summary", params={"project_id": "prj_none"}).json()
    assert other["metrics"]["tasks_completed"]["exactness"] == "missing"


def test_adaptive_below_threshold(client):
    seed_adaptive(client.ctx, verified=10, ok=8)
    r = client.get("/api/analytics/routing").json()
    (cell,) = r["cells"]
    assert (cell["verified_runs"], cell["successes"], cell["min_samples"]) == (10, 8, 20)
    assert cell["threshold_met"] is False
    assert cell["adjustment"]["points"] == 0.0 and cell["adjustment"]["applied"] is False
    assert "insufficient evidence" in cell["adjustment"]["explanation"]


def test_adaptive_above_threshold_and_settings(client):
    seed_adaptive(client.ctx, verified=10, ok=8)
    cur = client.get("/api/analytics/adaptive-settings").json()
    assert (cur["enabled"], cur["min_samples"], cur["max_adjustment"]) == (True, 20, 10.0)
    assert cur["effective_cap_points"] == 5.0  # router caps at scoring.historical_success
    put = client.put("/api/analytics/adaptive-settings", json={"min_samples": 5, "max_adjustment": 4}).json()
    assert (put["min_samples"], put["max_adjustment"], put["effective_cap_points"]) == (5, 4.0, 4.0)
    (cell,) = client.get("/api/analytics/routing").json()["cells"]
    assert cell["threshold_met"] is True and cell["adjustment"]["applied"] is True
    assert cell["adjustment"]["points"] == pytest.approx((0.8 - 0.5) / 0.5 * 4)  # 2.4
    assert "8/10" in cell["adjustment"]["explanation"]
    off = client.put("/api/analytics/adaptive-settings", json={"enabled": False}).json()
    assert off["enabled"] is False
    (cell,) = client.get("/api/analytics/routing").json()["cells"]
    assert cell["adjustment"]["applied"] is False and cell["adjustment"]["explanation"] == "adaptive routing disabled"


def test_adaptive_settings_bounds_and_storage(client):
    for bad in ({"min_samples": 1}, {"min_samples": 501}, {"max_adjustment": -1}, {"max_adjustment": 99}, {"enabled": "maybe"}, {}, {"x": 1}):
        assert client.put("/api/analytics/adaptive-settings", json=bad).status_code == 422, bad
    client.put("/api/analytics/adaptive-settings", json={"min_samples": 7})
    client.put("/api/analytics/adaptive-settings", json={"enabled": False})  # merges, keeps min_samples
    stored = client.ctx.db.read_sync(lambda tx: records.settings_all(tx))["routing.adaptive"]
    assert stored == {"min_samples": 7, "enabled": False}
    # how the routing service reads it
    eff = adaptive.effective_adaptive_cfg({"routing.adaptive": stored}, load_yaml("routing.yaml", client.ctx.config_dir))
    assert eff["min_samples"] == 7 and eff["enabled"] is False and eff["max_adjustment"] == 10.0
    # out-of-range stored values are ignored, never trusted
    eff = adaptive.effective_adaptive_cfg({"routing.adaptive": {"min_samples": 1, "max_adjustment": 1000}}, {"adaptive": {"min_samples": 20}})
    assert eff["min_samples"] == 20 and eff["max_adjustment"] == 10.0


def test_applied_history_and_cells_match_history_stats(client):
    t = seed_adaptive(client.ctx)
    for i, applied in enumerate((1, 0, 1)):
        S.sql(client.ctx, "INSERT INTO routing_decisions (id, task_id, phase, chosen_provider, chosen_model, candidates, reasons, rule_ids, adaptive_applied, created_at) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?)",
              (f"rd{i}", t, "execute", "claude", "m1", "[]", json.dumps(["tier fit", "adaptive: 8/10 verified successes"]), "[]", applied, f"{T}1{i}:00:00+00:00"))
    r = client.get("/api/analytics/routing").json()
    assert [h["id"] for h in r["applied_history"]] == ["rd2", "rd0"] and r["decisions_with_adaptive"] == 2
    assert r["applied_history"][0]["adaptive_reasons"] == ["adaptive: 8/10 verified successes"]
    hist = client.ctx.db.read_sync(telemetry.history_stats)
    assert {(c["provider"], c["model"], c["task_type"]): (c["verified_runs"], c["successes"]) for c in r["cells"]} == {
        k: (v["verified_runs"], v["successes"]) for k, v in hist.items()}
