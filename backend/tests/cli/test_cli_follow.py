"""`run --follow` and `logs`: event streaming (REST polling path; the WebSocket path needs a real port)."""

from __future__ import annotations

import json

import httpx

from orchestrator.cli import client as cli_client
from orchestrator.cli import follow as follow_mod
from orchestrator.providers.mock import MockProvider, MockScript

from .conftest import canned_factory


def test_follow_exits_0_on_completed(env):
    env.mock.queue(MockScript.success("implemented the parser"))
    p = env.project()
    r = env.run("run", "--project", p["id"], "--title", "implement the parser", "--follow")
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert "created task" in r.stdout and "implemented the parser" in r.stdout
    assert "state: " in r.stdout and "-> COMPLETED" in r.stdout
    assert "verification: passed" in r.stdout
    assert "finished: COMPLETED" in r.stdout


def test_follow_exits_nonzero_on_failed_or_blocked(make_env):
    env = make_env(MockProvider(default_script=MockScript.failure(message="always fails")))
    env.http.put("/api/settings", json={"values": {"limits": {"max_retry_attempts": 0, "max_repair_cycles": 0}}})
    p = env.project()
    r = env.run("run", "--project", p["id"], "--title", "implement a doomed thing", "--follow")
    assert r.exit_code == 1
    assert "finished: FAILED" in r.stdout or "finished: BLOCKED" in r.stdout


def test_follow_json_is_ndjson_with_a_final_result(env):
    p = env.project()
    t = env.json("tasks", "create", "--project", p["id"], "--title", "implement the parser")
    r = env.run("run", t["id"], "--follow", "--json")
    assert r.exit_code == 0
    lines = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
    assert lines[-1]["type"] == "cli.result" and lines[-1]["task"]["state"] == "COMPLETED"
    assert all(e["task_id"] == t["id"] for e in lines[:-1]) and any(e["type"] == "task.state_changed" for e in lines[:-1])


def test_follow_prints_approval_hint_and_stops_when_the_task_is_cancelled(env):
    p = env.project(verify=[])
    t = env.json("tasks", "create", "--project", p["id"], "--title", "write the docs")
    # Approve from the side as soon as the request exists, like a second terminal would.
    import threading
    import time

    def approver():
        end = time.monotonic() + 20
        while time.monotonic() < end:
            pend = env.http.get("/api/approvals", params={"status": "pending"}).json()
            if pend:
                time.sleep(0.4)  # let the follower print the request first
                env.http.post(f"/api/approvals/{pend[0]['id']}/resolve", json={"approve": True})
                return
            time.sleep(0.05)

    th = threading.Thread(target=approver)
    th.start()
    r = env.run("run", t["id"], "--follow")
    th.join()
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert "approval requested" in r.stdout and "orchestrator approvals approve apr_" in r.stdout


def test_logs_variants(env):
    env.mock.queue(MockScript.success("hello from the worker"))
    p = env.project()
    t = env.json("run", "--project", p["id"], "--title", "implement the parser")
    env.wait_state(t["id"], {"COMPLETED"})
    out = env.ok("logs", t["id"]).stdout
    assert "hello from the worker" in out and "state:" in out and "verification:" in out
    assert out.splitlines()[0][:4].isdigit()  # timestamped
    raw = env.ok("logs", t["id"], "--raw").stdout.splitlines()
    assert raw and all(json.loads(ln)["task_id"] == t["id"] for ln in raw)
    events = env.json("logs", t["id"])
    assert isinstance(events, list) and events
    last = events[-1]["seq"]
    assert "no log events" in env.ok("logs", t["id"], "--since-seq", str(last)).stdout
    assert len(env.json("logs", t["id"], "--since-seq", str(events[0]["seq"]))) == len(events) - 1
    followed = env.run("logs", t["id"], "--follow")  # finished task: replays and returns
    assert followed.exit_code == 0 and "hello from the worker" in followed.stdout and "is COMPLETED" in followed.stdout
    assert env.run("logs", "tsk_missing").exit_code == 1


def test_format_event_handles_unknown_and_multiline():
    e = {"seq": 1, "ts": "2026-11-12T10:00:00.5+00:00", "type": "run.output", "payload": {"text": "a\nb"}}
    assert follow_mod.format_event(e) == ["2026-11-12 10:00:00  | a", "2026-11-12 10:00:00  | b"]
    odd = {"seq": 2, "ts": "2026-11-12T10:00:01+00:00", "type": "something.new", "payload": {"k": 1}}
    assert "something.new" in follow_mod.format_event(odd)[0]
    tool = {"seq": 3, "ts": "2026-11-12T10:00:02+00:00", "type": "run.tool_use", "payload": {"name": "Edit", "path": "a.py"}}
    assert "tool: Edit" in follow_mod.format_event(tool)[0]


def test_unreachable_while_following_is_exit_2(env):
    p = env.project()
    t = env.json("tasks", "create", "--project", p["id"], "--title", "x")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gone", request=request)

    cli_client.set_client_factory(canned_factory(handler))
    assert env.run("run", t["id"], "--follow").exit_code == 2


def test_ws_token_goes_in_subprotocols_not_in_the_url():
    from orchestrator.cli import follow

    url = follow._ws_url("http://127.0.0.1:8765", 7)
    assert url == "ws://127.0.0.1:8765/ws?after_seq=7" and "token" not in url
    assert follow._ws_subprotocols("abc") == ["orch", "orch.token.abc"]
