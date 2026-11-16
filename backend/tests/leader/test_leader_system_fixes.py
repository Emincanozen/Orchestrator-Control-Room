"""Small system fixes: `{limits: null}` deletes the override; allowed origins follow the bind port."""

from __future__ import annotations

from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.api.security import BIND_PORT_ENV, effective_origins

BASE = "http://127.0.0.1:8765"


def _client(tmp_path):
    app = create_app(data_dir=tmp_path / "data", providers={}, start_scheduler=False)
    c = TestClient(app, base_url=BASE)
    c.__enter__()
    c.headers["Authorization"] = f"Bearer {app.state.rt.token}"
    return c


def test_null_limits_deletes_the_override(tmp_path):
    c = _client(tmp_path)
    try:
        assert c.put("/api/settings", json={"values": {"limits": {"max_repair_cycles": 1}}}).status_code == 200
        assert c.get("/api/settings").json()["overrides"]["limits"]["max_repair_cycles"] == 1
        r = c.put("/api/settings", json={"values": {"limits": None}})
        assert r.status_code == 200 and "limits" not in r.json()["overrides"]
        assert r.json()["effective"]["limits"]["max_repair_cycles"] == 3
        # a malformed non-null value is still rejected
        assert c.put("/api/settings", json={"values": {"limits": {"max_repair_cycles": -1}}}).status_code == 422
    finally:
        c.__exit__(None, None, None)


def test_origins_follow_the_bind_port(tmp_path, monkeypatch):
    monkeypatch.setenv(BIND_PORT_ENV, "9911")
    assert {"http://127.0.0.1:9911", "http://localhost:9911"} <= effective_origins(["http://127.0.0.1:8765"])
    c = _client(tmp_path)
    try:
        for origin in ("http://127.0.0.1:9911", "http://localhost:9911", "http://127.0.0.1:8765"):
            assert c.get("/api/status", headers={"Origin": origin}).status_code == 200
        assert c.get("/api/status", headers={"Origin": "http://127.0.0.1:9912"}).status_code == 403
        # still true after a settings write (rules are recomputed there)
        assert c.put("/api/settings", json={"values": {"limits": {"max_repair_cycles": 2}}}).status_code == 200
        assert c.get("/api/status", headers={"Origin": "http://localhost:9911"}).status_code == 200
    finally:
        c.__exit__(None, None, None)
