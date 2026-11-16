import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from skills_helpers import LocalPolicy, entry, make_repo, simple_skill_files, write_catalog

import orchestrator.skills as skills_pkg
from orchestrator.api.app import create_app
from orchestrator.api.routes import skills as skills_routes
from orchestrator.config import CONFIG_DIR

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def env(tmp_path):
    repos = {"alpha": tmp_path / "repos" / "alpha", "beta": tmp_path / "repos" / "beta"}
    sha = make_repo(repos["alpha"], simple_skill_files("alpha"))
    make_repo(repos["beta"], {**simple_skill_files("beta"), "scripts/go.sh": "echo\n"})
    cat = write_catalog(tmp_path / "cat.yaml", [
        entry("alpha", name="Alpha skill", user_label="the alpha one", category="writing"),
        entry("beta", category="dev"),
        entry("mcp1", kind="mcp_server", category="dev", source_url="local:alpha",
              install_method={"type": "mcp_npx", "command": "claude mcp add srv npx @x/srv", "notes": ""}),
        entry("app", kind="standalone_app", category="apps", install_method={"type": "manual", "command": "winget install app", "notes": "desktop"}),
        entry("ambiguous", source_url=None, uncertainty="which repo?", candidates=["https://github.com/a/b", "https://github.com/c/d"]),
    ])
    app = create_app(
        data_dir=tmp_path / "data", providers={}, start_scheduler=False, extra_routers=[skills_routes.router],
        on_startup=[skills_pkg.make_startup(catalog_path=cat, source_policy=LocalPolicy(repos), which=lambda n: f"/fake/{n}", platform="linux")],
    )
    with TestClient(app, base_url=BASE) as c:
        c.token = app.state.rt.token
        c.sha = sha
        c.tmp = tmp_path
        c.headers.update({"Authorization": f"Bearer {c.token}"})
        yield c


def mkproject(c, name="proj"):
    path = c.tmp / name
    path.mkdir()
    r = c.post("/api/projects", json={"name": name, "path": str(path)})
    assert r.status_code == 201, r.text
    return r.json()["id"], path


def test_auth_required_on_every_skills_route(env):
    anon = TestClient(env.app, base_url=BASE)
    for method, url in [("get", "/api/skills"), ("get", "/api/skills/alpha"), ("get", "/api/skills/alpha/content"), ("patch", "/api/skills/alpha"),
                        ("post", "/api/skills/alpha/install"), ("post", "/api/skills/alpha/activate"), ("post", "/api/skills/alpha/deactivate"),
                        ("post", "/api/skills/alpha/update"), ("post", "/api/skills/alpha/rollback")]:
        r = getattr(anon, method)(url)
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized", (method, url)
    bad = TestClient(env.app, base_url=BASE, headers={"Authorization": "Bearer nope-nope-nope-nope-nope-nope-nope"})
    assert bad.get("/api/skills").status_code == 401


def test_list_shape_filters_and_search(env):
    r = env.get("/api/skills")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list) and [x["id"] for x in rows] == ["alpha", "ambiguous", "app", "beta", "mcp1"]
    a = next(x for x in rows if x["id"] == "alpha")
    assert set(a) == {"id", "name", "user_label", "category", "kind", "description", "state", "source_url", "source_verified", "source", "installable",
                      "installable_reason", "activation", "pinned_ref", "installed_ref", "previous_ref", "compat", "risk", "requires_credentials",
                      "has_uncertainty", "last_error", "incompatible_reasons", "active_projects", "warnings", "updated_at"}
    assert a["state"] == "CATALOGUED_ON_DEMAND" and a["installable"] is True and a["installable_reason"] is None and a["source"] == "catalog"
    amb = next(x for x in rows if x["id"] == "ambiguous")
    assert amb["installable"] is False and "source URL unknown" in amb["installable_reason"] and amb["state"] == "CATALOGUED_ON_DEMAND" and amb["has_uncertainty"]
    assert "SKILL.md" not in json.dumps(rows)  # metadata only
    assert [x["id"] for x in env.get("/api/skills", params={"q": "ALPHA ONE"}).json()] == ["alpha"]
    assert [x["id"] for x in env.get("/api/skills", params={"category": "dev"}).json()] == ["beta", "mcp1"]
    assert [x["id"] for x in env.get("/api/skills", params={"kind": "mcp_server"}).json()] == ["mcp1"]
    assert env.get("/api/skills", params={"state": "INSTALLED_AND_ACTIVE"}).json() == []


def test_detail_shape_and_404(env):
    d = env.get("/api/skills/ambiguous").json()
    assert d["uncertainty"] == "which repo?" and d["candidates"] == ["https://github.com/a/b", "https://github.com/c/d"]
    assert {"security", "events", "activations", "instructions", "install_method", "dependencies", "permissions", "archives", "codex", "local_path"} <= set(d)
    assert d["activations"] == [] and d["events"] == [] and d["codex"]["supported"] is False
    assert d["security"]["inspected"] is False
    r = env.get("/api/skills/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_install_and_content_and_gating(env):
    assert env.get("/api/skills/alpha/content").status_code == 409  # not downloaded yet
    r = env.post("/api/skills/alpha/install")
    assert r.status_code == 200
    b = r.json()
    assert b["outcome"]["ok"] and b["outcome"]["ref"] == env.sha and b["outcome"]["state"] == "DOWNLOADED_AND_DISABLED"
    assert b["skill"]["state"] == "DOWNLOADED_AND_DISABLED" and b["skill"]["installed_ref"] == env.sha and b["skill"]["pinned_ref"] == env.sha
    assert b["skill"]["events"][0]["action"] == "download" and b["skill"]["events"][0]["to_state"] == "DOWNLOADED_AND_DISABLED"
    assert b["skill"]["security"]["inspected"] and b["skill"]["security"]["findings"] == []
    c = env.get("/api/skills/alpha/content").json()
    assert set(c) == {"skill_id", "ref", "path", "text", "truncated"} and "Do the thing." in c["text"]
    assert env.post("/api/skills/beta/install").json()["outcome"]["state"] == "NEEDS_SECURITY_REVIEW"
    r = env.post("/api/skills/ambiguous/install")
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_installable"
    assert env.post("/api/skills/ghost/install").status_code == 404


def test_patch_source_for_ambiguous_entry(env):
    r = env.patch("/api/skills/ambiguous", json={"source_url": "local:alpha"})
    assert r.status_code == 200
    s = r.json()["skill"]
    assert s["source_url"] == "local:alpha" and s["source_verified"] is False and s["source"] == "user" and s["installable"] is True
    assert s["state"] == "CATALOGUED_ON_DEMAND" and s["uncertainty"] == "which repo?"
    assert env.patch("/api/skills/ambiguous", json={"source_url": "https://evil.example.com/x/y"}).status_code == 422
    assert env.patch("/api/skills/ambiguous", json={"nope": 1}).status_code == 422
    assert env.post("/api/skills/ambiguous/install").json()["outcome"]["ok"]


def test_activation_flow_over_http(env):
    pid, path = mkproject(env)
    env.post("/api/skills/alpha/install")
    r = env.post("/api/skills/alpha/activate", json={"project_id": pid, "scope": "project"})
    assert r.status_code == 202
    out = r.json()["outcome"]
    assert out["status"] == "approval_required" and out["approval_id"].startswith("apr_") and out["ref"] == env.sha
    assert set(out) >= {"action", "ok", "status", "approval_id", "approval", "skill_id", "ref", "project_id", "message"}
    assert not (path / ".claude").exists()
    approvals = [a for a in env.get("/api/approvals", params={"status": "pending"}).json() if a["kind"] == "untrusted_tool_install"]
    assert [a["id"] for a in approvals] == [out["approval_id"]]
    assert env.post(f"/api/approvals/{out['approval_id']}/resolve", json={"approve": True}).status_code == 200
    r = env.post("/api/skills/alpha/activate", json={"project_id": pid})
    assert r.status_code == 200
    b = r.json()
    assert b["outcome"]["ok"] and b["outcome"]["changed"] and b["outcome"]["activation"]["project_id"] == pid
    assert (path / ".claude" / "skills" / "alpha" / "SKILL.md").is_file()
    assert b["skill"]["state"] == "INSTALLED_AND_ACTIVE" and b["skill"]["activations"][0]["artifacts"][0]["type"] == "skill_dir"
    # per-project view
    listed = {x["id"]: x for x in env.get("/api/skills", params={"project_id": pid}).json()}
    assert listed["alpha"]["project_activation"]["active"] is True and listed["alpha"]["project_activation"]["ref"] == env.sha
    assert listed["beta"]["project_activation"] == {"active": False}
    other, _ = mkproject(env, "other")
    assert next(x for x in env.get("/api/skills", params={"project_id": other}).json() if x["id"] == "alpha")["project_activation"] == {"active": False}
    assert env.get(f"/api/projects/{pid}").json()["skill_profile"]["active_skills"]["alpha"]["ref"] == env.sha
    # events carry skill.state_changed
    types = [e for e in env.get("/api/events", params={"limit": 500}).json() if e["type"] == "skill.state_changed"]
    assert [(e["payload"]["action"], e["payload"]["to_state"]) for e in types] == [("download", "DOWNLOADED_AND_DISABLED"), ("activate", "INSTALLED_AND_ACTIVE")]
    # deactivate
    r = env.post("/api/skills/alpha/deactivate", json={"project_id": pid})
    assert r.status_code == 200 and r.json()["skill"]["state"] == "DOWNLOADED_AND_DISABLED" and not (path / ".claude").exists()
    assert env.post("/api/skills/alpha/deactivate", json={"project_id": pid}).status_code == 409


def test_activate_validation_errors(env):
    pid, _ = mkproject(env)
    assert env.post("/api/skills/alpha/activate", json={}).status_code == 422
    assert env.post("/api/skills/alpha/activate", json={"project_id": pid, "scope": "global"}).status_code == 422
    r = env.post("/api/skills/alpha/activate", json={"project_id": pid})
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state"
    env.post("/api/skills/alpha/install")
    r = env.post("/api/skills/alpha/activate", json={"project_id": pid, "targets": ["codex"]})
    assert r.status_code == 409 and "unsupported" in r.json()["error"]["message"]
    assert env.post("/api/skills/alpha/activate", json={"project_id": "prj_x"}).status_code == 404


def test_mcp_over_http_and_update_rollback(env):
    pid, path = mkproject(env)
    env.post("/api/skills/mcp1/install")
    out = env.post("/api/skills/mcp1/activate", json={"project_id": pid}).json()["outcome"]
    env.post(f"/api/approvals/{out['approval_id']}/resolve", json={"approve": True})
    assert env.post("/api/skills/mcp1/activate", json={"project_id": pid}).status_code == 200
    assert json.loads((path / ".mcp.json").read_text())["mcpServers"]["srv"] == {"command": "npx", "args": ["@x/srv"]}
    assert env.post("/api/skills/mcp1/deactivate", json={"project_id": pid}).status_code == 200
    assert not (path / ".mcp.json").exists()
    # update with nothing new, rollback with nothing to roll back to
    assert env.post("/api/skills/mcp1/update").json()["outcome"]["changed"] is False
    r = env.post("/api/skills/mcp1/rollback")
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state"


def test_catalogue_only_error_has_instructions(env):
    pid, _ = mkproject(env)
    r = env.post("/api/skills/app/activate", json={"project_id": pid})
    assert r.status_code == 409 and r.json()["error"]["code"] == "catalogue_only"
    ins = r.json()["error"]["details"]["instructions"]
    assert ins["documented_method"]["command"] == "winget install app" and ins["activation"]["possible"] is False
    assert env.get("/api/skills/app").json()["instructions"]["documented_method"]["notes"] == "desktop"


def test_real_catalog_through_default_startup(tmp_path):
    app = create_app(data_dir=tmp_path / "d", providers={}, start_scheduler=False, extra_routers=[skills_routes.router], on_startup=[skills_pkg.startup])
    with TestClient(app, base_url=BASE) as c:
        c.headers["Authorization"] = f"Bearer {app.state.rt.token}"
        rows = c.get("/api/skills").json()
        assert len(rows) == 41 and sum(1 for r in rows if not r["installable"]) == 8
        assert app.state.rt.extras["skills_import"]["created"] == 41
        assert c.get("/api/skills", params={"q": "playwright"}).json()[0]["id"] == "playwright-mcp"
        d = c.get("/api/skills/remotion").json()
        assert d["candidates"] == ["https://github.com/remotion-dev/remotion"] and d["installable"] is True
        pw = c.get("/api/skills/playwright-mcp").json()
        assert pw["activation"]["possible"] and pw["activation"]["server"] == "playwright"
    # a second boot on the same data dir re-imports idempotently
    app2 = create_app(data_dir=tmp_path / "d", providers={}, start_scheduler=False, extra_routers=[skills_routes.router], on_startup=[skills_pkg.startup])
    with TestClient(app2, base_url=BASE) as c:
        assert app2.state.rt.extras["skills_import"]["unchanged"] == 41
