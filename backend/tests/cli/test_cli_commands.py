"""Happy paths of every data command against a real in-process app (mock provider)."""

from __future__ import annotations

import json

import httpx

from orchestrator.cli import client as cli_client
from orchestrator.providers.mock import MockProvider, MockScript

from .conftest import PY_OK, canned_factory


def test_status(env):
    r = env.ok("status")
    for label in ("health", "version", "scheduler", "leader", "tasks running", "tasks queued", "tasks blocked", "pending approvals", "last event seq"):
        assert label in r.stdout
    data = env.json("status")
    assert data["health"]["status"] == "ok" and "task_counts" in data["status"] and "pending_approvals" in data["status"]


def test_providers_models_usage(env):
    out = env.ok("providers").stdout
    assert "mock" in out and "installed" in out
    assert env.json("providers")[0]["provider"] == "mock"
    refreshed = env.json("providers", "refresh", "--provider", "mock")
    assert refreshed[0]["provider"] == "mock"
    assert env.run("providers", "refresh", "--provider", "nope").exit_code == 1
    models = env.json("models", "--provider", "mock")
    assert models and all(m["provider"] == "mock" for m in models)
    assert env.json("models", "--provider", "claude") == [] or all(m["provider"] == "claude" for m in env.json("models", "--provider", "claude"))
    assert "mock-model" in env.ok("models").stdout
    usage = env.ok("usage").stdout
    assert "exactness" in usage or "unknown" in usage
    assert isinstance(env.json("usage"), list)


def test_projects_lifecycle(env, tmp_path):
    from orchestrator.testing import make_git_repo

    repo = make_git_repo(tmp_path / "shop")
    added = json.loads(env.ok("projects", "add", "shop", str(repo), "--verify-cmd", PY_OK, "--json").stdout)
    assert added["name"] == "shop" and added["verify_commands"] == [PY_OK]
    pid = added["id"]
    assert pid in env.ok("projects", "list").stdout
    shown = env.ok("projects", "show", pid).stdout
    assert "shop" in shown and "verify commands" in shown
    assert env.json("projects", "show", pid)["id"] == pid
    assert env.ok("projects", "remove", pid).exit_code == 0
    assert env.json("projects", "list") == []
    assert env.run("projects", "show", pid).exit_code == 1


def test_tasks_create_list_show(env, tmp_path):
    p = env.project()
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\nDo the thing carefully.\n", encoding="utf-8")
    dep = env.json("tasks", "create", "--project", p["id"], "--title", "first")
    t = env.json(
        "tasks", "create", "--project", p["id"], "--title", "second", "--description-file", str(spec), "--type", "coding",
        "--role", "developer", "--priority", "70", "--depends-on", dep["id"], "--criteria", "works", "--criteria", "documented",
        "--verify-cmd", PY_OK, "--provider", "mock", "--model", "mock-model",
    )
    assert t["state"] == "CREATED" and t["priority"] == 70 and t["depends_on"] == [dep["id"]]
    assert t["acceptance_criteria"] == ["works", "documented"] and t["description"].startswith("# Spec")
    assert t["preferred_provider"] == "mock" and t["role"] == "developer" and t["verify_commands"] == [PY_OK]

    listing = env.ok("tasks", "list", "--project", p["id"]).stdout
    assert t["id"] in listing and dep["id"] in listing
    assert [x["id"] for x in env.json("tasks", "list", "--state", "CREATED")] and env.json("tasks", "list", "--state", "RUNNING") == []
    assert env.run("tasks", "list", "--state", "BOGUS").exit_code == 1
    detail = env.ok("tasks", "show", t["id"]).stdout
    assert "second" in detail and "acceptance criteria" in detail and "Do the thing" in detail
    j = env.json("tasks", "show", t["id"])
    assert set(j) == {"task", "runs", "verification"}

    r = env.run("tasks", "create", "--project", p["id"], "--title", "x", "--description", "a", "--description-file", str(spec))
    assert r.exit_code == 1 and "either" in r.stderr
    assert env.run("tasks", "create", "--project", p["id"], "--title", "x", "--description-file", str(tmp_path / "missing.md")).exit_code == 1


def test_routing_preview_and_decisions(env):
    out = env.ok("routing", "preview", "--title", "implement the repository layer").stdout
    assert "outcome" in out and "mock" in out and "candidates" in out
    j = env.json("routing", "preview", "--title", "implement the repository layer", "--phase", "plan", "--type", "coding")
    assert set(j) == {"outcome", "classification", "decision"}
    assert env.run("routing", "preview", "--title", "x", "--phase", "bogus").exit_code == 1
    assert env.json("routing", "decisions") == []
    # This checks routing persistence, not private-runtime warmup. Keep real
    # task completion/verification but use the checked-out fixture file.
    p = env.project(verify=[{"file_exists": "README.md"}])
    t = env.json("run", "--project", p["id"], "--title", "implement the repository layer")
    env.wait_state(t["id"], {"COMPLETED"})
    rows = env.json("routing", "decisions", "--task", t["id"])
    assert rows and rows[0]["chosen_provider"] == "mock"
    assert t["id"] in env.ok("routing", "decisions", "--task", t["id"]).stdout


def test_leader_show_real_app(env):
    r = env.ok("leader", "show")
    assert "leader" in r.stdout and "advisor" in r.stdout
    assert "leader" in env.json("leader", "show")


def test_skills_real_listing_and_show(env):
    rows = env.json("skills", "list")
    assert isinstance(rows, list)
    assert env.ok("skills", "list", "--query", "zzz-no-such-skill").stdout.strip() == "(none)"
    if rows:
        sid = rows[0]["id"]
        assert sid in env.ok("skills", "list").stdout
        assert env.json("skills", "show", sid)["id"] == sid
        assert sid in env.ok("skills", "show", sid).stdout
    assert env.run("skills", "show", "no-such-skill-id").exit_code == 1


def test_skills_actions_send_the_right_requests(env):
    seen: list[httpx.Request] = []
    detail = {"id": "s1", "state": "installed"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/activate") and json.loads(request.content).get("project_id") == "prj_needs_approval":
            return httpx.Response(202, json={"outcome": {"action": "activate", "ok": False, "status": "approval_required", "approval_id": "apr_77",
                                                          "project_id": "prj_needs_approval", "message": "pending"}, "skill": detail})
        return httpx.Response(200, json={"outcome": {"action": path.rsplit("/", 1)[-1], "ok": True}, "skill": detail})

    cli_client.set_client_factory(canned_factory(handler))
    for action in ("install", "update", "rollback"):
        assert env.ok("skills", action, "s1").exit_code == 0
        assert seen[-1].url.path == f"/api/skills/s1/{action}" and seen[-1].method == "POST"
    env.ok("skills", "update", "s1", "--ref", "abc")
    assert json.loads(seen[-1].content) == {"ref": "abc"}
    env.ok("skills", "deactivate", "s1", "--project", "prj_1")
    assert json.loads(seen[-1].content) == {"project_id": "prj_1"}
    env.ok("skills", "activate", "s1", "--project", "prj_1", "--target", "codex")
    assert json.loads(seen[-1].content) == {"project_id": "prj_1", "targets": ["codex"]}
    r = env.ok("skills", "activate", "s1", "--project", "prj_needs_approval")
    assert "apr_77" in r.stdout and "orchestrator approvals approve apr_77" in r.stdout
    assert json.loads(env.ok("skills", "activate", "s1", "--project", "prj_needs_approval", "--json").stdout)["outcome"]["approval_id"] == "apr_77"
    assert env.run("skills", "activate", "s1").exit_code == 1  # --project required


def test_approvals_list_approve_deny(env):
    p = env.project(verify=[])  # no verify commands -> the task needs an approval to be accepted
    t = env.json("run", "--project", p["id"], "--title", "write the docs")
    env.wait_state(t["id"], {"NEEDS_APPROVAL"})
    pending = env.json("approvals", "list")
    assert len(pending) == 1 and pending[0]["task_id"] == t["id"]
    aid = pending[0]["id"]
    assert aid in env.ok("approvals", "list").stdout
    assert aid in env.ok("approvals", "list", "--task", t["id"]).stdout
    r = env.ok("approvals", "approve", aid, "--note", "ok by test")
    assert "approved" in r.stdout
    env.wait_state(t["id"], {"COMPLETED"})
    assert env.json("approvals", "list") == []
    done = env.json("approvals", "list", "--status", "all")
    assert done[0]["status"] == "approved" and done[0]["note"] == "ok by test"

    t2 = env.json("run", "--project", p["id"], "--title", "write more docs")
    env.wait_state(t2["id"], {"NEEDS_APPROVAL"})
    aid2 = env.json("approvals", "list")[0]["id"]
    assert "denied" in env.ok("approvals", "deny", aid2).stdout
    assert env.run("approvals", "approve", "apr_missing").exit_code == 1


def test_run_validation(env):
    p = env.project()
    t = env.json("tasks", "create", "--project", p["id"], "--title", "x")
    r = env.run("run", t["id"], "--title", "mixed")
    assert r.exit_code == 1 and "either" in r.stderr
    assert env.run("run", "--project", p["id"]).exit_code == 1
    assert env.run("run", "tsk_missing").exit_code == 1


def test_run_by_id_starts_the_task(env):
    p = env.project()
    t = env.json("tasks", "create", "--project", p["id"], "--title", "implement the parser")
    out = env.ok("run", t["id"]).stdout
    assert "started task" in out
    env.wait_state(t["id"], {"COMPLETED"})


def test_control_commands_pause_resume_cancel(env, make_env):
    env.mock.queue(MockScript.hang(60))
    p = env.project()
    t = env.json("run", "--project", p["id"], "--title", "implement a slow thing")
    env.wait_state(t["id"], {"PLANNING", "RUNNING"})
    r = env.ok("pause", t["id"])
    assert "paused" in r.stdout
    env.wait_state(t["id"], {"PAUSED"})
    r = env.ok("resume", t["id"])
    assert "resumed" in r.stdout and "retried" not in r.stdout
    env.wait_state(t["id"], {"COMPLETED"})  # the second run uses the default (successful) script
    assert env.run("resume", t["id"]).exit_code == 1  # COMPLETED: nothing to resume
    c = env.json("tasks", "create", "--project", p["id"], "--title", "never mind")
    assert "cancelled" in env.ok("cancel", c["id"]).stdout
    assert env.http.get(f"/api/tasks/{c['id']}").json()["state"] == "CANCELLED"
    assert env.run("pause", "tsk_missing").exit_code == 1


def test_resume_retries_failed_tasks(make_env):
    env = make_env(MockProvider(default_script=MockScript.failure(message="always fails")))
    env.http.put("/api/settings", json={"values": {"limits": {"max_retry_attempts": 0, "max_repair_cycles": 0}}})
    p = env.project()
    t = env.json("run", "--project", p["id"], "--title", "implement a doomed thing")
    env.wait_state(t["id"], {"FAILED", "BLOCKED"})
    r = env.ok("resume", t["id"])
    assert "retried" in r.stdout
    env.wait_state(t["id"], {"FAILED", "BLOCKED"})

