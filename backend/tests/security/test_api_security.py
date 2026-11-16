import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from orchestrator.api.app import create_app
from orchestrator.providers.mock import MockProvider
from orchestrator.security.auth import load_or_create_token, token_matches

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def client(tmp_path):
    app = create_app(data_dir=tmp_path / "data", providers={"mock": MockProvider()}, celebrate_seconds=0, poll_interval=0.05)
    with TestClient(app, base_url=BASE) as c:
        c.token = app.state.rt.token
        yield c


def auth(c):
    return {"Authorization": f"Bearer {c.token}"}


def test_token_file_created_once(tmp_path):
    p = tmp_path / "api-token"
    t1 = load_or_create_token(p)
    t2 = load_or_create_token(p)
    assert t1 == t2 and len(t1) >= 32
    assert token_matches(t1, t1) and not token_matches(t1, t1[:-1] + "x") and not token_matches(t1, None)


def test_health_is_public(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


@pytest.mark.parametrize("path", ["/api/status", "/api/tasks", "/api/projects", "/api/agents", "/api/events", "/api/approvals", "/api/settings"])
def test_token_required(client, path):
    r = client.get(path)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    r = client.get(path, headers={"Authorization": "Bearer wrong-token-wrong-token-wrong-token"})
    assert r.status_code == 401
    r = client.get(path, headers={"Authorization": f"Basic {client.token}"})
    assert r.status_code == 401
    assert client.get(path, headers=auth(client)).status_code == 200


def test_bad_host_rejected(client):
    r = client.get("/api/health", headers={"Host": "evil.example:8765"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "invalid_host"


def test_origin_checks(client):
    r = client.get("/api/status", headers={**auth(client), "Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "invalid_origin"
    r = client.get("/api/status", headers={**auth(client), "Origin": "http://127.0.0.1:8765"})
    assert r.status_code == 200
    # the Vite dev origin (5173) is only accepted with ORCHESTRATOR_DEV=1 (see test_sec_16_dev_surface.py)
    r = client.get("/api/status", headers={**auth(client), "Origin": "http://127.0.0.1:5173"})
    assert r.status_code == 403


def test_ws_requires_token(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("ws://127.0.0.1:8765/ws?token=nope") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_ws_rejects_bad_origin(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"ws://127.0.0.1:8765/ws?token={client.token}", headers={"Origin": "http://evil.example"}) as ws:
            ws.receive_json()
    assert exc.value.code == 4403


def test_artifact_path_traversal_rejected(client, tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e.invalid"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True)
    (tmp_path / "secret.txt").write_text("top secret")
    h = auth(client)
    pid = client.post("/api/projects", json={"name": "r", "path": str(repo)}, headers=h).json()["id"]
    tid = client.post("/api/tasks", json={"project_id": pid, "title": "t"}, headers=h).json()["id"]
    # give the task a workspace pointing at the project (in-place style) for the file endpoint
    rt = client.app.state.rt
    client.portal.call(rt.tasks.set_fields, tid, {"worktree_path": str(repo)})
    ok = client.get(f"/api/tasks/{tid}/artifacts", params={"path": "f.txt"}, headers=h)
    assert ok.status_code == 200 and ok.json()["content"] == "x"
    for bad in ["../secret.txt", "..\\secret.txt", str(tmp_path / "secret.txt"), "\\\\server\\share\\x", "f.txt:ads"]:
        r = client.get(f"/api/tasks/{tid}/artifacts", params={"path": bad}, headers=h)
        assert r.status_code == 403, bad
        assert r.json()["error"]["code"] == "path_policy"


def test_project_path_validation(client, tmp_path):
    h = auth(client)
    for bad in ["relative/dir", "\\\\server\\share", str(tmp_path / "missing")]:
        r = client.post("/api/projects", json={"name": "x", "path": bad}, headers=h)
        assert r.status_code == 403, bad
    rt = client.app.state.rt
    for inside in (rt.ctx.paths.logs_dir, rt.ctx.paths.worktrees_dir):  # both exist after startup
        assert inside.is_dir()
        r = client.post("/api/projects", json={"name": "x", "path": str(inside)}, headers=h)
        assert r.status_code == 403, inside
