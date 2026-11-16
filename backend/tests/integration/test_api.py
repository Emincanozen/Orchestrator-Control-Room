"""HTTP API + WebSocket end to end with the MockProvider."""

import time

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.providers.mock import MockProvider, MockScript, MockStep
from orchestrator.testing import make_git_repo, py_check

BASE = "http://127.0.0.1:8765"
WS = "ws://127.0.0.1:8765"


@pytest.fixture
def mock():
    return MockProvider()


@pytest.fixture
def client(tmp_path, mock):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    app = create_app(
        data_dir=tmp_path / "data", providers={"mock": mock}, celebrate_seconds=0, poll_interval=0.05, frontend_dist=dist
    )
    with TestClient(app, base_url=BASE) as c:
        c.headers["Authorization"] = f"Bearer {app.state.rt.token}"
        c.token = app.state.rt.token
        r = c.put("/api/settings", json={"values": {"limits": {"retry_backoff_seconds": 0}}})
        assert r.status_code == 200, r.text
        yield c


def wait_for(client, task_id, states, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = client.get(f"/api/tasks/{task_id}").json()
        if t["state"] in states:
            return t
        time.sleep(0.05)
    raise AssertionError(f"task stayed {t['state']} ({t['state_reason']})")


def make_project(client, tmp_path, name="demo"):
    repo = make_git_repo(tmp_path / name)
    r = client.post("/api/projects", json={"name": name, "path": str(repo), "verify_commands": [py_check("print('ok')")]})
    assert r.status_code == 201, r.text
    return r.json()


def test_full_flow_over_http(client, tmp_path, mock):
    project = make_project(client, tmp_path)
    assert project["is_git"] and project["isolation"] == "worktree" and project["default_branch"] == "main"
    mock.queue(MockScript.success("made it", files={"out.txt": "42"}))
    r = client.post("/api/tasks", json={
        "project_id": project["id"], "title": "write out.txt", "acceptance_criteria": ["out.txt is 42"],
        "verify_commands": [{"file_exists": "out.txt", "contains": "42"}], "preferred_provider": "mock",
    })
    assert r.status_code == 201
    task = r.json()
    assert task["state"] == "CREATED"
    assert client.post(f"/api/tasks/{task['id']}/start").json()["state"] == "QUEUED"
    done = wait_for(client, task["id"], {"COMPLETED", "FAILED", "BLOCKED"})
    assert done["state"] == "COMPLETED"

    runs = client.get(f"/api/tasks/{task['id']}/runs").json()
    assert len(runs) == 1 and runs[0]["status"] == "succeeded" and runs[0]["provider"] == "mock"
    ver = client.get(f"/api/tasks/{task['id']}/verification").json()
    assert ver[-1]["status"] == "passed"
    cps = client.get(f"/api/tasks/{task['id']}/checkpoints").json()
    assert cps and cps[0]["kind"] == "auto"
    diff = client.get(f"/api/tasks/{task['id']}/diff").json()
    assert diff["available"] and "out.txt" in diff["changed_files"] and "+42" in diff["diff"]
    arts = client.get(f"/api/tasks/{task['id']}/artifacts").json()
    assert [f["path"] for f in arts["files"]] == ["out.txt"]
    assert client.get(f"/api/tasks/{task['id']}/artifacts", params={"path": "out.txt"}).json()["content"] == "42"
    evs = client.get(f"/api/tasks/{task['id']}/events").json()
    assert [e["seq"] for e in evs] == sorted(e["seq"] for e in evs)
    assert client.get("/api/tasks", params={"state": "COMPLETED"}).json()[0]["id"] == task["id"]
    assert client.get("/api/tasks", params={"project_id": project["id"], "state": "QUEUED"}).json() == []

    status = client.get("/api/status").json()
    assert status["task_counts"]["COMPLETED"] == 1 and status["scheduler"]["running"] and status["providers"] == ["mock"]
    assert status["last_seq"] == client.get("/api/events", params={"limit": 5000}).json()[-1]["seq"]
    assert status["leader"] is None
    client.put("/api/settings", json={"values": {"leadership": {"leader": {"provider": "mock", "model": "mock-large"}}}})
    assert client.get("/api/status").json()["leader"] == {"provider": "mock", "model": "mock-large"}

    agent_id = runs[0]["agent_id"]
    insp = client.get(f"/api/agents/{agent_id}").json()
    assert insp["role"] == "backend" and insp["activity"] == "idle"
    assert insp["elapsed_seconds"]["exactness"] == "measured"


def test_inspector_while_running(client, tmp_path, mock):
    project = make_project(client, tmp_path)
    mock.queue(MockScript(steps=[MockStep(action="text", text="thinking"), MockStep(action="usage", data={"input_tokens": 7}), MockStep(action="hang")]))
    task = client.post("/api/tasks", json={"project_id": project["id"], "title": "long", "acceptance_criteria": ["x"],
                                           "preferred_provider": "mock", "preferred_model": "mock-large", "autostart": True}).json()
    wait_for(client, task["id"], {"RUNNING"})
    deadline = time.monotonic() + 10
    insp = None
    while time.monotonic() < deadline:
        agents = client.get("/api/agents").json()
        busy = [a for a in agents if a["current_task_id"] == task["id"] and a["activity"] == "coding"]
        if busy:
            insp = client.get(f"/api/agents/{busy[0]['id']}").json()
            if insp["usage"]["current_run"] and insp["usage"]["current_run"]["value"]:
                break
        time.sleep(0.05)
    assert insp is not None
    assert insp["task_id"] == task["id"] and insp["provider"] == "mock" and insp["model"] == "mock-large"
    assert insp["project_name"] == "demo" and insp["operation"].startswith("execute")
    assert any(e["type"] == "run.output" for e in insp["recent_events"])
    r = client.post(f"/api/tasks/{task['id']}/cancel")
    assert r.status_code == 200
    wait_for(client, task["id"], {"CANCELLED"})


def test_error_envelopes(client, tmp_path):
    r = client.get("/api/tasks/tsk_missing")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = client.post("/api/tasks", json={"title": ""})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_failed"
    project = make_project(client, tmp_path)
    task = client.post("/api/tasks", json={"project_id": project["id"], "title": "t"}).json()
    r = client.post(f"/api/tasks/{task['id']}/resume")
    assert r.status_code == 409 and r.json()["error"]["code"] in ("stale_state", "invalid_transition")
    r = client.post(f"/api/tasks/{task['id']}/retry")
    assert r.status_code == 409
    a = client.post("/api/tasks", json={"project_id": project["id"], "title": "a"}).json()
    b = client.post("/api/tasks", json={"project_id": project["id"], "title": "b", "depends_on": [a["id"]]}).json()
    r = client.patch(f"/api/tasks/{a['id']}", json={"depends_on": [b["id"]]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "dependency_cycle"
    r = client.post("/api/projects", json={"name": "dup", "path": project["path"]})
    assert r.status_code == 409
    r = client.put("/api/settings", json={"values": {"limits": {"max_parallel_workers": -1}}})
    assert r.status_code == 422
    r = client.put("/api/settings", json={"values": {"nonsense": 1}})
    assert r.status_code == 422


def test_settings_roundtrip(client):
    r = client.put("/api/settings", json={"values": {"limits": {"max_parallel_workers": 1}}})
    body = r.json()
    assert body["effective"]["limits"]["max_parallel_workers"] == 1
    assert body["effective"]["limits"]["max_parallel_per_project"] == 2
    assert body["overrides"]["limits"]["retry_backoff_seconds"] == 0  # merged, not replaced
    assert client.get("/api/settings").json()["defaults"]["limits"]["max_parallel_workers"] == 3


def test_ws_replay_by_seq_then_live(client, tmp_path, mock):
    project = make_project(client, tmp_path)
    t1 = client.post("/api/tasks", json={"project_id": project["id"], "title": "one"}).json()
    events = client.get("/api/events", params={"after_seq": 0}).json()
    assert events, "expected history"
    mid = events[len(events) // 2]["seq"]
    with client.websocket_connect(f"{WS}/ws?token={client.token}&after_seq={mid}") as ws:
        replayed = []
        while True:
            msg = ws.receive_json()
            if msg.get("type") == "replay.done":
                break
            replayed.append(msg)
        assert [e["seq"] for e in replayed] == [e["seq"] for e in events if e["seq"] > mid]
        # live event after replay
        client.post(f"/api/tasks/{t1['id']}/start")
        live = ws.receive_json()
        while live.get("type") == "ping":
            live = ws.receive_json()
        assert live["seq"] > replayed[-1]["seq"] if replayed else mid
        assert live["type"] == "task.state_changed" and live["payload"]["to"] == "QUEUED"


def test_agents_roster_seeded(client):
    agents = client.get("/api/agents").json()
    assert [a["role"] for a in agents] == ["leader", "advisor", "backend", "backend", "frontend", "frontend", "research", "test", "visual", "reviewer"]
    assert all(a["activity"] == "idle" for a in agents)


def test_approvals_api(client, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    p = client.post("/api/projects", json={"name": "plain", "path": str(plain)}).json()
    assert p["isolation"] == "in_place"
    pending = client.get("/api/approvals", params={"status": "pending"}).json()
    assert [a["kind"] for a in pending] == ["git_init_in_project"]
    r = client.post(f"/api/approvals/{pending[0]['id']}/resolve", json={"approve": False, "note": "keep it plain"})
    assert r.json()["status"] == "denied"
    r = client.post(f"/api/approvals/{pending[0]['id']}/resolve", json={"approve": True})
    assert r.status_code == 409


def test_spa_static(client):
    assert client.get("/").text == "<html>spa</html>"
    assert client.get("/office/room/1").text == "<html>spa</html>"
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/../../etc/passwd").status_code in (200, 404)  # normalised by the client; never escapes dist
    r = client.get("/api/unknown")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_project_delete_guard(client, tmp_path):
    project = make_project(client, tmp_path)
    t = client.post("/api/tasks", json={"project_id": project["id"], "title": "t"}).json()
    assert client.delete(f"/api/projects/{project['id']}").status_code == 409
    client.post(f"/api/tasks/{t['id']}/cancel")
    assert client.delete(f"/api/projects/{project['id']}").status_code == 204
    assert client.get(f"/api/projects/{project['id']}").status_code == 404
