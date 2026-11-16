"""HTTP surface of M3 (auth, shapes, status.leader) and office-seat activity of leader/advisor runs."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.db import records
from orchestrator.domain.states import AgentActivity, TaskState as S
from orchestrator.providers.mock import MockProvider
from orchestrator.services.providers import mock_models_cfg
from orchestrator.testing import make_git_repo

from .conftest import Brain

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def brain():
    return Brain()


@pytest.fixture
def client(tmp_path, brain):
    providers = {"mock": MockProvider(scripter=brain.for_provider("mock")),
                 "other": MockProvider(name="other", model="other-model", scripter=brain.for_provider("other"))}
    app = create_app(data_dir=tmp_path / "data", providers=providers, use_routing=True, use_leader=True,
                     extra_models_cfg=mock_models_cfg("other", "other-model"), celebrate_seconds=0, poll_interval=0.05)
    with TestClient(app, base_url=BASE) as c:
        c.headers["Authorization"] = f"Bearer {app.state.rt.token}"
        c.anon = TestClient(app, base_url=BASE)
        c.tmp = tmp_path
        yield c


def project(client):
    path = make_git_repo(client.tmp / "proj")
    r = client.post("/api/projects", json={"name": "p", "path": str(path)})
    assert r.status_code in (200, 201), r.text
    return r.json()


ENDPOINTS = [
    ("GET", "/api/leadership", None),
    ("PUT", "/api/leadership", {"advisor_policy": "auto"}),
    ("POST", "/api/leadership/switch", {"provider": "other", "model": "other-model"}),
    ("POST", "/api/advisor/consult", {"problem": "x"}),
    ("GET", "/api/decisions", None),
    ("POST", "/api/decisions/dec_x/export", None),
    ("POST", "/api/projects/prj_x/objectives", {"text": "x"}),
    ("GET", "/api/projects/prj_x/objectives", None),
    ("GET", "/api/objectives/obj_x", None),
    ("GET", "/api/skills", None),
]


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_endpoints_require_the_token(client, method, path, body):
    r = client.anon.request(method, path, json=body)
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


def test_get_leadership_shape_and_default_leader(client):
    r = client.get("/api/leadership")
    assert r.status_code == 200
    j = r.json()
    assert set(j) == {"leader", "advisor", "advisor_effective", "settings", "role_minimum_tier", "budgets", "permissions",
                      "candidates", "assignment_paused", "switch_in_progress"}
    assert j["leader"]["reason"] == "default" and j["leader"]["source"] == "default" and j["leader"]["provider"] in ("mock", "other")
    assert j["advisor"] is None and j["advisor_effective"] == "router"
    assert j["settings"]["advisor_policy"] == "on_request" and j["settings"]["max_leader_calls_per_task"] == 6
    assert j["role_minimum_tier"] == {"leader": "advanced", "advisor": "advanced"}
    assert j["budgets"]["consultations_today"] == {"used": 0, "limit": 10}
    assert {m["provider"] for m in j["candidates"]["leader"] if m["eligible"]} == {"mock", "other"}
    assert set(j["permissions"]) == {"approvals_required_for", "auto_approve", "worker_permissions"}
    assert client.get("/api/status").json()["leader"] == {"provider": j["leader"]["provider"], "model": j["leader"]["model"]}


def test_put_leadership_and_validation_errors(client):
    r = client.put("/api/leadership", json={"advisor_policy": "auto", "max_consultations_per_day": 4,
                                            "advisor": {"provider": "other", "model": "other-model"}})
    assert r.status_code == 200 and r.json()["advisor"] == {"provider": "other", "model": "other-model"}
    assert r.json()["settings"]["advisor_policy"] == "auto" and r.json()["budgets"]["consultations_today"]["limit"] == 4
    assert client.put("/api/leadership", json={"advisor": {"provider": "other", "model": "ghost"}}).status_code == 422
    assert client.put("/api/leadership", json={"leader": {"provider": "x", "model": "y"}}).status_code == 422  # identity: switch only
    assert client.put("/api/leadership", json={"advisor_policy": "sometimes"}).status_code == 422
    assert client.put("/api/leadership", json={"advisor": None}).json()["advisor"] is None


def test_switch_endpoint_shapes(client):
    cur = client.get("/api/leadership").json()["leader"]
    other = "other" if cur["provider"] == "mock" else "mock"
    model = "other-model" if other == "other" else "mock-model"
    r = client.post("/api/leadership/switch", json={"provider": other, "model": model})
    assert r.status_code == 200
    assert set(r.json()) == {"status", "from", "to", "checkpoint_id", "validated", "skip_validation", "attempts", "reason"}
    assert r.json()["status"] == "switched" and r.json()["to"] == {"provider": other, "model": model}
    assert client.get("/api/status").json()["leader"]["provider"] == other
    assert client.post("/api/leadership/switch", json={"provider": other, "model": model}).status_code == 422  # already the leader
    assert client.post("/api/leadership/switch", json={"provider": "nope", "model": "x"}).status_code == 422


def test_switch_rollback_is_a_409_with_details(client, brain):
    cur = client.get("/api/leadership").json()["leader"]
    other = "other" if cur["provider"] == "mock" else "mock"
    wrong = {"tasks": [{"id": "tsk_bogus", "next_action": "x"}]}
    brain.push("switch", wrong, wrong)
    pid = project(client)["id"]
    t = client.post("/api/tasks", json={"project_id": pid, "title": "pending", "autostart": False}).json()
    client.post(f"/api/tasks/{t['id']}/start")
    client.put("/api/settings", json={"values": {"limits": {"max_parallel_workers": 1}}})
    r = client.post("/api/leadership/switch", json={"provider": other, "model": "other-model" if other == "other" else "mock-model"})
    # the pending task may already have been picked up; the answer ids never match either way
    assert r.status_code == 409 and r.json()["error"]["code"] == "leader_switch_failed"
    d = r.json()["error"]["details"]
    assert d["status"] == "rolled_back" and d["attempts"] == 2 and "state mismatch" in d["reason"]
    assert client.get("/api/status").json()["leader"]["provider"] == cur["provider"]


def test_consult_endpoint_decisions_and_export(client):
    pid = project(client)["id"]
    t = client.post("/api/tasks", json={"project_id": pid, "title": "t"}).json()
    r = client.post("/api/advisor/consult", json={"task_id": t["id"], "problem": "cache?", "options": ["a", "b"], "constraints": ["c"]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert set(j) == {"decision_id", "run_id", "trigger", "provider", "model", "independence", "evidence_status", "result", "budget"}
    assert set(j["result"]) == {"assessment", "recommended_option", "risks", "disagreements", "confidence", "evidence_requested"}
    assert j["independence"] in ("different_provider", "same_provider") and j["trigger"] == "user"
    decs = client.get("/api/decisions", params={"task_id": t["id"]}).json()
    assert len(decs) == 1
    assert set(decs[0]) >= {"id", "project_id", "task_id", "title", "context", "options", "decision", "rationale", "evidence",
                            "advisor_run_id", "kind", "evidence_status", "meta", "created_at"}
    assert client.get("/api/decisions", params={"project_id": "nope"}).json() == []
    ex = client.post(f"/api/decisions/{j['decision_id']}/export")
    assert ex.status_code == 200 and ex.json()["appended"] is True and ex.json()["path"].replace("\\", "/").endswith("docs/DECISIONS.md")
    assert client.post(f"/api/decisions/{j['decision_id']}/export").status_code == 409
    assert client.post("/api/decisions/dec_missing/export").status_code == 404
    # budget refusal is a clear 429
    client.put("/api/leadership", json={"max_consultations_per_day": 1})
    r = client.post("/api/advisor/consult", json={"problem": "again"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "advisor_budget_exhausted"
    assert r.json()["error"]["details"]["reason"] == "budget_per_day"
    client.put("/api/leadership", json={"advisor_policy": "never"})
    r = client.post("/api/advisor/consult", json={"problem": "again"})
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "disabled"


def test_objective_endpoints(client):
    pid = project(client)["id"]
    r = client.post(f"/api/projects/{pid}/objectives", json={"text": "Add hello.txt", "wait": True})
    assert r.status_code == 201, r.text
    o = r.json()
    assert set(o) == {"id", "project_id", "text", "status", "deliverables", "acceptance_criteria", "prerequisites", "error",
                      "unsafe_verify_commands", "run_ids", "tasks", "task_counts", "created_at", "updated_at"}
    assert o["status"] == "planned" and len(o["tasks"]) == 2
    assert set(o["tasks"][0]) == {"id", "key", "title", "state", "task_type", "role", "priority", "depends_on", "verify_commands"}
    assert client.get(f"/api/objectives/{o['id']}").json()["id"] == o["id"]
    assert [x["id"] for x in client.get(f"/api/projects/{pid}/objectives").json()] == [o["id"]]
    assert client.get("/api/objectives/obj_missing").status_code == 404
    assert client.post("/api/projects/prj_missing/objectives", json={"text": "x"}).status_code == 404
    # tasks start only on request; then the objective turns running
    client.post(f"/api/tasks/{o['tasks'][0]['id']}/start")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and client.get(f"/api/objectives/{o['id']}").json()["status"] == "planned":
        time.sleep(0.05)
    assert client.get(f"/api/objectives/{o['id']}").json()["status"] in ("running", "failed", "completed")


def test_events_carry_the_documented_shapes(client):
    pid = project(client)["id"]
    cur = client.get("/api/leadership").json()["leader"]
    other = "other" if cur["provider"] == "mock" else "mock"
    client.post("/api/leadership/switch", json={"provider": other, "model": "other-model" if other == "other" else "mock-model"})
    j = client.post("/api/advisor/consult", json={"problem": "p"}).json()
    ev = client.get("/api/events", params={"limit": 500}).json()
    by = {}
    for e in ev:
        by.setdefault(e["type"], e)
    lc = [e for e in ev if e["type"] == "leader.changed"]
    assert {"from", "to", "reason", "source", "status", "validated", "skip_validation", "checkpoint_id", "attempts"} <= set(lc[-1]["payload"])
    assert {"decision_id", "trigger", "provider", "model", "independence", "recommended_option", "confidence", "evidence_status", "run_id"} == set(
        by["advisor.consulted"]["payload"])
    assert {"decision_id", "kind", "title", "decision", "evidence_status", "evidence", "advisor_run_id", "flags"} == set(by["decision.recorded"]["payload"])
    assert j["decision_id"] == by["decision.recorded"]["payload"]["decision_id"]
    del pid


def test_default_app_serves_the_skills_router(client):
    assert client.get("/api/skills").status_code == 200


# ------------------------------------------------------------------ seats
def seat(h, agent_id):
    return h.ctx.db.read_sync(lambda tx: records.agent_get(tx, agent_id))


async def test_leader_and_advisor_seats_show_work_only_while_the_run_is_live(lh):
    p = await lh.project()
    seen: dict[str, list] = {"plan": [], "advisor": []}
    lh.brain.hooks["plan"] = lambda req: seen["plan"].append((seat(lh, "agt_leader").activity, seat(lh, "agt_leader").current_run_id is not None,
                                                              seat(lh, "agt_backend_1").activity))
    lh.brain.hooks["advisor"] = lambda req: seen["advisor"].append((seat(lh, "agt_advisor").activity, seat(lh, "agt_leader").activity))
    t = await lh.mk_task(p.id, title="t", verify_commands=[["python", "-c", "print(1)"]])
    await lh.tasks.transition(t.id, S.QUEUED, reason="s")
    assert await lh.run_once(t.id) == S.COMPLETED
    # during the plan run: leader seat PLANNING with a live run; the worker seat is not 'planning'
    assert seen["plan"] == [(AgentActivity.PLANNING, True, AgentActivity.IDLE)]
    await lh.stack.advisor.consult(trigger="user", problem="q")
    assert seen["advisor"] == [(AgentActivity.CONSULTING, AgentActivity.IDLE)]
    # after the runs: both seats are idle, nothing keeps a working activity without a live run
    assert seat(lh, "agt_leader").activity == AgentActivity.IDLE and seat(lh, "agt_leader").current_run_id is None
    assert seat(lh, "agt_advisor").activity == AgentActivity.IDLE and seat(lh, "agt_advisor").current_run_id is None
    changes = [e for e in lh.events("agent.activity_changed") if e["agent_id"] in ("agt_leader", "agt_advisor")]
    assert [(e["agent_id"], e["payload"]["to"]) for e in changes] == [
        ("agt_leader", "planning"), ("agt_leader", "idle"), ("agt_advisor", "consulting"), ("agt_advisor", "idle")]


async def test_review_run_shows_reviewing_on_the_leader_seat(lh):
    await lh.require_review()
    p = await lh.project()
    seen = []
    lh.brain.hooks["review"] = lambda req: seen.append((seat(lh, "agt_leader").activity, seat(lh, "agt_leader").current_run_id is not None))
    t = await lh.mk_task(p.id, title="t", acceptance_criteria=["c"], plan={"x": 1}, verify_commands=[["python", "-c", "print(1)"]])
    await lh.tasks.transition(t.id, S.QUEUED, reason="s")
    assert await lh.run_once(t.id) == S.COMPLETED
    assert seen == [(AgentActivity.REVIEWING, True)] and seat(lh, "agt_leader").activity == AgentActivity.IDLE


async def test_objective_decomposition_shows_planning_on_the_leader_seat(lh):
    p = await lh.project()
    seen = []
    lh.brain.hooks["decompose"] = lambda req: seen.append(seat(lh, "agt_leader").activity)
    await lh.stack.objectives.create(p.id, "x", wait=True)
    assert seen == [AgentActivity.PLANNING] and seat(lh, "agt_leader").activity == AgentActivity.IDLE


def _wait_leader_set(client, timeout=10.0):
    deadline = time.monotonic() + timeout
    st = client.get("/api/status").json()
    while st["leader_state"] != "set" and time.monotonic() < deadline:
        assert st["leader"] is None and st["leader_state"] in ("detecting", "none")  # never a half-truth
        time.sleep(0.05)
        st = client.get("/api/status").json()
    return st


def test_status_reports_leader_state_and_the_default_leader_event(client):
    st = _wait_leader_set(client)
    assert st["leader_state"] == "set" and st["leader"] is not None
    events = client.get("/api/events", params={"limit": 500}).json()
    items = events["events"] if isinstance(events, dict) else events
    changed = [e for e in items if e["type"] == "leader.changed"]
    assert len(changed) == 1 and changed[0]["payload"]["reason"] == "default" and "cheapest" in changed[0]["payload"]["detail"]


def _bare_client(tmp_path, monkeypatch, ensure):
    from orchestrator.leader.service import LeadershipService

    monkeypatch.setattr(LeadershipService, "ensure_default_leader", ensure)
    providers = {"mock": MockProvider(scripter=Brain().for_provider("mock"))}
    app = create_app(data_dir=tmp_path / "data", providers=providers, use_routing=True, use_leader=True,
                     celebrate_seconds=0, poll_interval=0.05)
    c = TestClient(app, base_url=BASE)
    return app, c


def test_status_leader_is_null_and_detecting_while_the_default_is_being_recorded(tmp_path, monkeypatch):
    import asyncio

    async def never(self):
        await asyncio.sleep(3600)

    app, c = _bare_client(tmp_path, monkeypatch, never)
    with c:
        c.headers["Authorization"] = f"Bearer {app.state.rt.token}"
        st = c.get("/api/status").json()
        assert st["leader"] is None and st["leader_state"] == "detecting"


def test_status_leader_state_is_none_when_no_default_could_be_recorded(tmp_path, monkeypatch):
    async def nothing(self):
        self.default_state = "none"
        return None

    app, c = _bare_client(tmp_path, monkeypatch, nothing)
    with c:
        c.headers["Authorization"] = f"Bearer {app.state.rt.token}"
        deadline = time.monotonic() + 10
        st = c.get("/api/status").json()
        while st["leader_state"] == "detecting" and time.monotonic() < deadline:
            time.sleep(0.05)
            st = c.get("/api/status").json()
        assert st["leader"] is None and st["leader_state"] == "none"
