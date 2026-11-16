"""SECURITY_REVIEW #16 (backend part): /docs, /redoc, /openapi.json and the 5173 dev origins need ORCHESTRATOR_DEV=1."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.api.security import DEV_ENV, effective_origins
from orchestrator.providers.mock import MockProvider

BASE = "http://127.0.0.1:8765"
DEV_ORIGIN = "http://127.0.0.1:5173"


def _client(tmp_path):
    # no built dashboard: otherwise the SPA fallback answers unknown GETs with index.html (200, but not Swagger)
    app = create_app(data_dir=tmp_path / "data", providers={"mock": MockProvider()}, start_scheduler=False, frontend_dist=tmp_path / "no-dist")
    return app, TestClient(app, base_url=BASE)


def test_docs_and_schema_are_not_served_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv(DEV_ENV, raising=False)
    app, c = _client(tmp_path)
    with c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            for headers in ({}, {"Authorization": f"Bearer {app.state.rt.token}"}):
                r = c.get(path, headers=headers)
                assert r.status_code == 404, (path, r.status_code)


def test_spa_fallback_never_serves_swagger_or_schema(tmp_path, monkeypatch):
    monkeypatch.delenv(DEV_ENV, raising=False)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa</html>")
    app = create_app(data_dir=tmp_path / "data", providers={"mock": MockProvider()}, start_scheduler=False, frontend_dist=dist)
    with TestClient(app, base_url=BASE) as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            body = c.get(path).text.lower()
            assert "swagger" not in body and '"openapi"' not in body and "redoc" not in body


def test_dev_flag_enables_docs_and_dev_origin(tmp_path, monkeypatch):
    monkeypatch.setenv(DEV_ENV, "1")
    app, c = _client(tmp_path)
    with c:
        assert c.get("/openapi.json").status_code == 200
        assert c.get("/docs").status_code == 200
        r = c.get("/api/status", headers={"Authorization": f"Bearer {app.state.rt.token}", "Origin": DEV_ORIGIN})
        assert r.status_code == 200


def test_dev_origin_rejected_without_flag(tmp_path, monkeypatch):
    monkeypatch.delenv(DEV_ENV, raising=False)
    app, c = _client(tmp_path)
    with c:
        r = c.get("/api/status", headers={"Authorization": f"Bearer {app.state.rt.token}", "Origin": DEV_ORIGIN})
        assert r.status_code == 403 and r.json()["error"]["code"] == "invalid_origin"


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_only_exact_one_enables_dev_mode(monkeypatch, value):
    monkeypatch.setenv(DEV_ENV, value)
    assert DEV_ORIGIN not in effective_origins(["http://127.0.0.1:8765"])


def test_default_policy_no_longer_lists_dev_origins():
    from orchestrator.config import load_yaml

    origins = load_yaml("policies.yaml")["security"]["allowed_origins"]
    assert not any(":5173" in o for o in origins)
