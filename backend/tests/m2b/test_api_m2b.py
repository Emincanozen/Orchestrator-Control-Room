"""HTTP surface of M2b: providers, models, usage, routing, preferences; provider wiring env switches."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from orchestrator.api import app as app_module
from orchestrator.api.app import create_app
from orchestrator.providers.mock import MockProvider, MockScript, MockStep
from orchestrator.testing import make_git_repo, py_check

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def mock():
    return MockProvider()


@pytest.fixture
def client(tmp_path, mock):
    app = create_app(data_dir=tmp_path / "data", providers={"mock": mock}, use_routing=True, celebrate_seconds=0, poll_interval=0.05)
    with TestClient(app, base_url=BASE) as c:
        c.headers["Authorization"] = f"Bearer {app.state.rt.token}"
        c.anon = TestClient(app, base_url=BASE)
        assert c.put("/api/settings", json={"values": {"limits": {"retry_backoff_seconds": 0}}}).status_code == 200
        yield c


def _wait(client, task_id, states, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        t = client.get(f"/api/tasks/{task_id}").json()
        if t["state"] in states:
            return t
        time.sleep(0.05)
    raise AssertionError(t)


ENDPOINTS = [
    ("GET", "/api/providers", None),
    ("POST", "/api/providers/refresh", None),
    ("GET", "/api/models", None),
    ("GET", "/api/usage", None),
    ("GET", "/api/routing/decisions", None),
    ("POST", "/api/routing/preview", {"title": "x"}),
    ("GET", "/api/routing/preferences", None),
    ("PUT", "/api/routing/preferences", {"provider_order": ["mock"]}),
]


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_endpoints_require_the_token(client, method, path, body):
    r = client.anon.request(method, path, json=body)
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


def test_providers_shape_and_refresh(client):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not client.get("/api/providers").json()[0]["detected"]:
        time.sleep(0.05)  # startup detection runs in the background
    body = client.get("/api/providers").json()
    assert isinstance(body, list) and [p["provider"] for p in body] == ["mock"]
    p = body[0]
    assert set(p) == {"provider", "detected", "installed", "version", "executable", "authenticated", "auth_detail", "error",
                      "capabilities", "billing_class", "checked_at", "model_count", "usage"}
    assert p["installed"] is True and p["billing_class"] == "local" and p["model_count"] == 1
    assert p["capabilities"]["structured_stream"] is True
    assert set(p["usage"]) == {"state", "reason", "resets_at", "until", "confidence", "evidence_metric"}
    refreshed = client.post("/api/providers/refresh", json={"provider": "mock"})
    assert refreshed.status_code == 200 and refreshed.json()[0]["provider"] == "mock"
    assert client.post("/api/providers/refresh").status_code == 200
    assert client.post("/api/providers/refresh", json={"provider": "nope"}).status_code == 404
    # persisted + event emitted
    events = client.get("/api/events", params={"limit": 500}).json()
    assert any(e["type"] == "provider.status" and e["payload"]["provider"] == "mock" for e in events)


def test_models_shape(client):
    client.post("/api/providers/refresh")
    models = client.get("/api/models").json()
    m = next(x for x in models if x["provider"] == "mock")
    assert set(m) == {"provider", "model_id", "alias", "display_name", "tier", "tier_source", "capabilities", "listed_capabilities",
                      "billing_class", "verification", "source", "discovered", "provider_installed"}
    assert m["model_id"] == "mock-model" and m["discovered"] is True and "coding" in m["capabilities"]


def test_usage_reflects_events_with_exactness(client, mock, tmp_path):
    repo = make_git_repo(tmp_path / "demo")
    project = client.post("/api/projects", json={"name": "demo", "path": str(repo), "verify_commands": [py_check("print('ok')")]}).json()
    reset = (datetime.now(UTC) + timedelta(hours=3)).replace(microsecond=0).isoformat()
    mock.queue(MockScript(steps=[
        MockStep(action="rate_limit", data={"status": "allowed_warning", "utilization": 0.9, "window": "five_hour", "resets_at": reset}),
        MockStep(action="usage", data={"input_tokens": 11, "output_tokens": 4, "cost_usd": 0.02}),
        MockStep(action="succeed", text="ok"),
    ]))
    t = client.post("/api/tasks", json={"project_id": project["id"], "title": "implement the repository layer",
                                         "acceptance_criteria": ["x"], "autostart": True}).json()
    assert _wait(client, t["id"], {"COMPLETED", "FAILED", "BLOCKED"})["state"] == "COMPLETED"
    usage = client.get("/api/usage").json()
    entry = next(e for e in usage if e["provider"] == "mock" and e["model"] is None)
    same = lambda a, b: datetime.fromisoformat(a) == datetime.fromisoformat(b)  # noqa: E731 - stored with microseconds
    assert entry["usage_state"]["state"] == "LOW" and same(entry["usage_state"]["resets_at"], reset)
    util = next(v for v in entry["values"] if v["metric"] == "window_utilization.five_hour")
    assert util == {**util, "value": 0.9, "unit": "ratio", "exactness": "exact", "source": "mock.rate_limit_event",
                    "state": "LOW", "model": None, "confidence": "high"}
    assert same(util["resets_at"], reset)
    assert util["observed_at"]
    model_entry = next(e for e in usage if e["provider"] == "mock" and e["model"] == "mock-model")
    cost = next(v for v in model_entry["values"] if v["metric"] == "cost_usd")
    assert cost["exactness"] == "estimated" and cost["unit"] == "usd_list_price_equivalent"
    events = client.get("/api/events", params={"limit": 500}).json()
    assert any(e["type"] == "usage.updated" and e["payload"]["usage_state"]["state"] == "LOW" for e in events)


def test_usage_lists_provider_without_data_as_unknown(client):
    entry = next(e for e in client.get("/api/usage").json() if e["provider"] == "mock")
    assert entry["usage_state"]["state"] in ("UNKNOWN",) or entry["values"]  # nothing synthesised
    if not entry["values"]:
        assert entry["usage_state"]["state"] == "UNKNOWN"


def test_routing_decisions_and_preview(client, tmp_path):
    repo = make_git_repo(tmp_path / "demo")
    project = client.post("/api/projects", json={"name": "demo", "path": str(repo), "verify_commands": [py_check("print('ok')")]}).json()
    t = client.post("/api/tasks", json={"project_id": project["id"], "title": "implement the repository layer",
                                         "acceptance_criteria": ["x"], "autostart": True}).json()
    _wait(client, t["id"], {"COMPLETED", "FAILED", "BLOCKED"})
    rows = client.get("/api/routing/decisions", params={"task_id": t["id"]}).json()
    assert len(rows) == 1
    d = rows[0]
    assert set(d) == {"id", "task_id", "run_id", "phase", "chosen_provider", "chosen_model", "candidates", "reasons", "rule_ids",
                      "adaptive_applied", "detail", "created_at"}
    assert d["chosen_provider"] == "mock" and d["candidates"][0]["score"] > 0 and d["detail"]["classification"]["task_type"] == "coding"
    assert client.get("/api/routing/decisions", params={"limit": 1}).json()[0]["id"] == d["id"]
    assert client.get("/api/routing/decisions", params={"task_id": "tsk_missing"}).json() == []
    assert client.get("/api/routing/decisions", params={"limit": 0}).status_code == 422

    before = client.get("/api/events", params={"limit": 500}).json()
    pv = client.post("/api/routing/preview", json={"title": "implement the repository layer"})
    assert pv.status_code == 200
    body = pv.json()
    assert set(body) == {"outcome", "classification", "decision"} and body["outcome"] == "route"
    assert body["decision"]["provider"] == "mock" and body["classification"]["task_type"] == "coding"
    assert body["decision"]["candidates"][0]["components"]["tier_fit"] > 0
    blocked = client.post("/api/routing/preview", json={"title": "generate an image logo", "phase": "plan"}).json()
    assert blocked["outcome"] == "blocked" and blocked["decision"]["provider"] is None and blocked["decision"]["rejected"]
    # no side effects: no new decision rows, no new routing events
    assert len(client.get("/api/routing/decisions").json()) == 1
    after = client.get("/api/events", params={"limit": 500}).json()
    assert [e["seq"] for e in after if e["type"] == "router.decision"] == [e["seq"] for e in before if e["type"] == "router.decision"]
    assert client.post("/api/routing/preview", json={"title": ""}).status_code == 422
    assert client.post("/api/routing/preview", json={"title": "x", "phase": "bogus"}).status_code == 422


def test_preferences_get_put(client):
    base = client.get("/api/routing/preferences").json()
    assert set(base) == {"provider_order", "disabled_providers", "disabled_models", "defaults", "overridden"}
    assert base["provider_order"] == ["claude", "codex", "antigravity"] and base["overridden"] == []  # routing.yaml defaults
    r = client.put("/api/routing/preferences", json={"provider_order": ["codex", " claude ", "codex"], "disabled_models": ["gpt-6.1-sol"]})
    assert r.status_code == 200
    v = r.json()
    assert v["provider_order"] == ["codex", "claude"] and v["disabled_models"] == ["gpt-6.1-sol"]
    assert v["overridden"] == ["disabled_models", "provider_order"] and v["defaults"]["provider_order"] == ["claude", "codex", "antigravity"]
    assert client.get("/api/routing/preferences").json() == v
    reset = client.put("/api/routing/preferences", json={"provider_order": None, "disabled_models": None}).json()
    assert reset["overridden"] == [] and reset["provider_order"] == ["claude", "codex", "antigravity"]
    assert client.put("/api/routing/preferences", json={"provider_order": [""]}).status_code == 422
    assert client.put("/api/routing/preferences", json={"bogus": 1}).status_code == 422
    # preferences affect routing
    client.put("/api/routing/preferences", json={"disabled_providers": ["mock"]})
    out = client.post("/api/routing/preview", json={"title": "implement the repository layer"}).json()
    assert out["outcome"] == "blocked" and any(
        r["provider"] == "mock" and r["filter"] == "disabled" for r in out["decision"]["rejected"])


# ---- provider wiring switches -------------------------------------------------------------------------------


def test_disable_real_providers_env_builds_no_real_adapter(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_DISABLE_REAL_PROVIDERS", "1")
    monkeypatch.delenv("ORCHESTRATOR_ENABLE_MOCK", raising=False)
    assert app_module.default_registry().names() == []
    assert app_module.default_providers() == {}
    monkeypatch.setenv("ORCHESTRATOR_ENABLE_MOCK", "1")
    assert app_module.default_registry().names() == ["mock"]


def test_real_providers_are_the_default_without_the_env(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_DISABLE_REAL_PROVIDERS", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_ENABLE_MOCK", raising=False)
    assert app_module.default_registry().names() == ["claude", "codex", "antigravity"]  # constructed only; no detect()


def test_app_with_env_has_no_providers_and_still_starts(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_DISABLE_REAL_PROVIDERS", "1")
    monkeypatch.delenv("ORCHESTRATOR_ENABLE_MOCK", raising=False)
    app = create_app(data_dir=tmp_path / "data", celebrate_seconds=0)  # providers=None -> production wiring
    with TestClient(app, base_url=BASE) as c:
        c.headers["Authorization"] = f"Bearer {app.state.rt.token}"
        assert c.get("/api/providers").json() == []
        assert c.get("/api/status").json()["providers"] == []
        # routing is real in this wiring: nothing to route to -> blocked, with the router's reason
        out = c.post("/api/routing/preview", json={"title": "implement the repository layer"}).json()
        assert out["outcome"] == "blocked" and out["decision"]["provider"] is None


def test_startup_detection_failure_does_not_fail_startup(tmp_path):
    class Broken(MockProvider):
        async def detect(self):
            raise RuntimeError("boom")

    app = create_app(data_dir=tmp_path / "data", providers={"mock": Broken()}, celebrate_seconds=0)
    with TestClient(app, base_url=BASE) as c:
        c.headers["Authorization"] = f"Bearer {app.state.rt.token}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not c.get("/api/providers").json()[0]["detected"]:
            time.sleep(0.05)
        p = c.get("/api/providers").json()[0]
        assert p["installed"] is False and "detect failed" in p["error"]
