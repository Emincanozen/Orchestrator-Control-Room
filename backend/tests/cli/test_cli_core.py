"""CLI core: client errors, exit codes, token secrecy, ASCII output, serve / dashboard / stop / doctor."""

from __future__ import annotations

import json
import subprocess
import sys
import time

import httpx
import psutil
import pytest

from orchestrator.cli import client as cli_client
from orchestrator.cli import doctor as doctor_mod
from orchestrator.cli import main as main_mod
from orchestrator.cli import server as server_mod
from orchestrator.cli.client import ApiError
from orchestrator.cli.main import app as cli_app
from orchestrator.providers.base import ProviderStatus

from .conftest import BASE, canned_factory


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


# ---- exit codes and error rendering ------------------------------------------------------------
def test_backend_unreachable_exits_2_with_hint(env):
    cli_client.set_client_factory(canned_factory(_unreachable))
    for args in (["status"], ["projects", "list"], ["tasks", "list"], ["providers"]):
        r = env.run(*args)
        assert r.exit_code == 2, args
        assert "orchestrator serve" in r.stderr and "orchestrator dashboard" in r.stderr
        assert r.stdout == ""


def test_unreachable_without_token_file_is_still_unreachable(tmp_path):
    # nothing was ever started: no token file, nothing listening -> exit 2, not a token error
    from typer.testing import CliRunner

    cli_client.set_client_factory(canned_factory(_unreachable))
    r = CliRunner().invoke(cli_app, ["status"])
    assert r.exit_code == 2 and "orchestrator dashboard" in r.stderr
    r = CliRunner().invoke(cli_app, ["projects", "list"])
    assert r.exit_code == 2 and "orchestrator dashboard" in r.stderr


def test_missing_token_file_with_running_backend_is_a_clear_error(env, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_TOKEN_FILE", str(tmp_path / "nope"))
    r = env.run("projects", "list")
    assert r.exit_code == 1 and "token file not found" in r.stderr


def test_401_gets_a_specific_message(env, tmp_path, monkeypatch):
    bad = tmp_path / "bad-token"
    bad.write_text("x" * 40, encoding="ascii")
    monkeypatch.setenv("ORCHESTRATOR_TOKEN_FILE", str(bad))
    r = env.run("projects", "list")
    assert r.exit_code == 1
    assert "401" in r.stderr and "ORCHESTRATOR_DATA_DIR" in r.stderr
    assert "x" * 40 not in r.stderr + r.stdout


def test_error_envelope_is_rendered(env):
    r = env.run("tasks", "show", "tsk_missing")
    assert r.exit_code == 1
    assert "tsk_missing" in r.stderr and "[not_found]" in r.stderr
    r = env.run("projects", "add", "x", str(env.tmp / "does-not-exist"))
    assert r.exit_code == 1 and r.stderr.startswith("error:")


def test_usage_errors_exit_1(env):
    assert env.run("tasks", "create", "--title", "x").exit_code == 1  # no --project
    assert env.run("run").exit_code == 1
    assert env.run("approvals", "list", "--status", "bogus").exit_code == 1
    assert env.run("no-such-command").exit_code == 1


def test_url_precedence_flag_over_env(env, monkeypatch):
    seen: list[str] = []
    cli_client.set_client_factory(lambda base: (seen.append(base), env.http)[1])
    monkeypatch.setenv("ORCHESTRATOR_URL", "http://127.0.0.1:9100/")
    env.ok("status")
    env.ok("--url", "http://127.0.0.1:9200", "status")
    env.ok("status", "--url", "http://localhost:9300")
    assert seen[0] == "http://127.0.0.1:9100"
    assert "http://127.0.0.1:9200" in seen and "http://localhost:9300" in seen
    monkeypatch.delenv("ORCHESTRATOR_URL")
    seen.clear()
    env.ok("status")
    assert seen[0] == "http://127.0.0.1:8765"  # policies default


def test_token_is_not_accepted_on_the_command_line(env):
    assert env.run("--token", "abc", "status").exit_code != 0
    assert env.run("status", "--token", "abc").exit_code != 0


def test_token_never_printed_anywhere(env, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(main_mod.webbrowser, "open", lambda u: opened.append(u) or True)
    monkeypatch.setattr(doctor_mod, "detect_providers", lambda: {})
    env.project()
    for args in (["status"], ["projects", "list"], ["providers"], ["usage"], ["dashboard"], ["doctor"]):
        for extra in ([], ["--json"]):
            env.run(*args, *extra)  # Env.run asserts the token is absent from stdout and stderr
    assert opened and env.token in opened[0]  # ...but it is in the browser URL


# ---- output safety ------------------------------------------------------------------------------
def test_json_flag_global_and_local_are_valid_json(env):
    env.project()
    for args in (["status"], ["providers"], ["models"], ["usage"], ["projects", "list"], ["tasks", "list"], ["approvals", "list"],
                 ["routing", "decisions"], ["skills", "list"], ["leader", "show"]):
        local = env.json(*args)
        global_ = json.loads(env.ok("--json", *args).stdout)
        assert type(local) is type(global_)
    st = env.json("status")
    assert "health" in st and "status" in st


def test_ascii_and_no_ansi_when_not_a_tty(env, monkeypatch):
    monkeypatch.setattr(doctor_mod, "detect_providers", lambda: {})
    p = env.project("café-déjà")
    t = env.http.post("/api/tasks", json={"project_id": p["id"], "title": "Fix “quotes” — 日本語 \x1b[31mred"}).json()
    for args in (["tasks", "list"], ["projects", "list"], ["tasks", "show", t["id"]], ["doctor"]):
        r = env.run(*args)
        assert r.stdout.isascii(), (args, r.stdout)
        assert "\x1b" not in r.stdout
        assert not any(ch in r.stdout for ch in "─│┌└┃━")  # no box-drawing characters
    out = env.ok("tasks", "list").stdout
    assert t["id"] in out  # ids are never truncated
    data = env.json("tasks", "list")  # JSON is lossless (escaped), not replaced
    assert data[0]["title"].startswith("Fix “quotes”")
    assert env.ok("tasks", "list", "--json").stdout.isascii()


def test_unknown_values_are_never_invented(env):
    assert "unknown" in env.ok("usage").stdout.lower()
    assert "unknown" in env.ok("providers").stdout.lower()


# ---- optional endpoints ----------------------------------------------------------------------
def _missing(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"error": {"code": "not_found", "message": "not found", "details": {}}})


def test_missing_endpoints_print_not_available(env):
    cli_client.set_client_factory(canned_factory(_missing))
    cases = [
        ["leader", "show"], ["leader", "set", "--provider", "p", "--model", "m"],
        ["advisor", "consult", "--problem", "why?"], ["knowledge", "search", "x"], ["analytics", "summary"],
    ]
    for args in cases:
        r = env.run(*args)
        assert r.exit_code == 0, (args, r.stdout, r.stderr)
        assert "not available in this build" in r.stdout
        assert json.loads(env.ok(*args, "--json").stdout)["available"] is False


def test_real_resource_404_is_still_an_error():
    assert not ApiError(404, "not_found", "task tsk_x not found").is_missing_endpoint()
    assert ApiError(404, "not_found", "not found").is_missing_endpoint()
    assert ApiError(400, "error", "knowledge service is not started (register ...)").is_missing_endpoint()


def test_leader_advisor_knowledge_analytics_requests(env):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/leadership/switch":
            return httpx.Response(200, json={"status": "switched", "from": {"provider": "a"}, "to": {"provider": "claude"}})
        if request.url.path == "/api/advisor/consult":
            return httpx.Response(200, json={"decision_id": "dec_1", "provider": "codex", "model": "m", "result": {"answer": "yes"}})
        if request.url.path == "/api/knowledge/search":
            return httpx.Response(200, json={"results": [{"kind": "doc", "title": "Readme", "source": "README.md", "text": "hello"}], "used": []})
        if request.url.path == "/api/analytics/summary":
            return httpx.Response(200, json={"tasks": {"completed": {"value": None, "exactness": "missing", "source": "x", "n": 0}}})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "not found", "details": {}}})

    cli_client.set_client_factory(canned_factory(handler))
    assert "switched" in env.ok("leader", "set", "--provider", "claude", "--model", "opus").stdout
    assert json.loads(seen[-1].content) == {"provider": "claude", "model": "opus"}
    assert "dec_1" in env.ok("advisor", "consult", "--task", "tsk_1", "--problem", "which?").stdout
    assert json.loads(seen[-1].content) == {"problem": "which?", "task_id": "tsk_1"}
    out = env.ok("knowledge", "search", "hello", "--project", "prj_1", "--limit", "3").stdout
    assert "Readme" in out and json.loads(seen[-1].content) == {"query": "hello", "limit": 3, "project_id": "prj_1"}
    assert "unknown" in env.ok("analytics", "summary").stdout  # a missing metric is 'unknown', never a number
    assert env.run("advisor", "consult").exit_code == 1  # --problem required


# ---- serve ------------------------------------------------------------------------------------------
def test_serve_refuses_non_loopback_without_the_flag(env, monkeypatch):
    import uvicorn

    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
    r = env.run("serve", "--host", "0.0.0.0")
    assert r.exit_code == 1 and not calls
    assert "--i-understand-exposing-the-dashboard" in r.stderr and "loopback" in r.stderr
    r = env.run("serve", "--host", "192.168.1.20", "--port", "9000")
    assert r.exit_code == 1 and not calls


def test_serve_defaults_and_explicit_exposure(env, monkeypatch):
    import uvicorn

    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(main_mod, "_probe_instance_lock", lambda: None)  # the fixture app holds the data-dir lock
    monkeypatch.setenv("ORCHESTRATOR_BIND_PORT", "")  # monkeypatch restores it after serve overwrites it
    assert env.run("serve").exit_code == 0
    assert calls[-1]["host"] == "127.0.0.1" and calls[-1]["port"] == 8765 and calls[-1]["loop"] == "asyncio"
    assert env.run("serve", "--port", "9123", "--no-browser").exit_code == 0
    assert calls[-1]["port"] == 9123 and calls[-1]["access_log"] is True
    assert env.run("serve", "--no-access-log").exit_code == 0 and calls[-1]["access_log"] is False
    r = env.run("serve", "--host", "0.0.0.0", "--i-understand-exposing-the-dashboard")
    assert r.exit_code == 0 and calls[-1]["host"] == "0.0.0.0" and "WARNING" in r.stderr


# ---- dashboard / stop ------------------------------------------------------------------------------
def test_dashboard_when_already_running_opens_browser_with_token(env, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(main_mod.webbrowser, "open", lambda u: opened.append(u) or True)
    r = env.run("dashboard")
    assert r.exit_code == 0 and "already running" in r.stdout and f"{BASE}/" in r.stdout
    assert opened == [f"{BASE}/#token={env.token}"]
    assert "?token" not in r.stdout
    opened.clear()
    r = env.run("dashboard", "--no-browser", "--json")
    assert not opened and json.loads(r.stdout)["browser_opened"] is False


def test_dashboard_starts_a_detached_server_when_not_running(env, monkeypatch, tmp_path):
    state = {"spawned": 0}
    down = httpx.Client(base_url=BASE, transport=httpx.MockTransport(_unreachable))
    cli_client.set_client_factory(lambda base: env.http if state["spawned"] else down)

    class FakeProc:
        pid = 987654

        def poll(self):
            return None

    def fake_spawn(port):
        state["spawned"] += 1
        state["port"] = port
        return FakeProc()

    monkeypatch.setattr(server_mod, "spawn_server", fake_spawn)
    monkeypatch.setattr(server_mod, "port_in_use", lambda port, host="127.0.0.1": False)
    monkeypatch.setattr(main_mod.webbrowser, "open", lambda u: True)
    r = env.run("dashboard", "--port", "8791", "--no-browser")
    assert r.exit_code == 0, r.stderr
    assert state == {"spawned": 1, "port": 8791} and "started" in r.stdout
    rec = server_mod.read_pid_file()
    assert rec is not None and rec.pid == 987654 and rec.port == 8791
    assert (tmp_path / "data" / "server.pid").is_file()


def test_background_server_is_spawned_detached_without_access_log(monkeypatch, tmp_path):
    seen: dict = {}

    class P:
        pid = 1

    def fake_popen(argv, **kw):
        seen.update(argv=argv, kw=kw)
        return P()

    monkeypatch.setattr(server_mod.subprocess, "Popen", fake_popen)
    server_mod.spawn_server(8795)
    assert seen["argv"][1:3] == ["-m", "orchestrator.cli.main"] and "--no-access-log" in seen["argv"] and "8795" in seen["argv"]
    assert seen["kw"]["stdin"] is subprocess.DEVNULL and seen["kw"]["env"]["PYTHONUNBUFFERED"] == "1"
    assert (tmp_path / "data" / "logs" / "server.log").exists()


def test_dashboard_refuses_a_port_used_by_something_else(env, monkeypatch):
    cli_client.set_client_factory(canned_factory(_unreachable))
    monkeypatch.setattr(server_mod, "port_in_use", lambda port, host="127.0.0.1": True)
    monkeypatch.setattr(server_mod, "spawn_server", lambda port: pytest.fail("must not spawn"))
    r = env.run("dashboard", "--no-browser")
    assert r.exit_code == 1 and "in use" in r.stderr


def _sleeper(*marker: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", *marker], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_gone(pid: int, timeout: float = 15.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not psutil.pid_exists(pid):
            return True
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.Error:
            return True
        time.sleep(0.1)
    return False


def test_stop_without_pid_file_is_a_noop(env):
    r = env.run("stop")
    assert r.exit_code == 0 and "nothing to stop" in r.stdout


def test_stop_terminates_only_a_verified_orchestrator_serve_process(env):
    proc = _sleeper("orchestrator.cli.main", "serve")
    try:
        server_mod.write_pid_file(proc.pid, 8791)
        r = env.run("stop")
        assert r.exit_code == 0 and f"pid {proc.pid}" in r.stdout
        proc.wait(timeout=15)
        assert _wait_gone(proc.pid)
        assert not (env.tmp / "data" / "server.pid").exists()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_stop_refuses_a_foreign_process_and_leaves_it_alive(env):
    proc = _sleeper("some-other-program")
    try:
        server_mod.write_pid_file(proc.pid, 8791)
        r = env.run("stop")
        assert r.exit_code == 1 and "refusing" in r.stderr
        time.sleep(0.3)
        assert proc.poll() is None  # untouched
        assert (env.tmp / "data" / "server.pid").is_file()
    finally:
        proc.kill()


def test_stop_ignores_a_recycled_pid(env):
    proc = _sleeper("orchestrator.cli.main", "serve")
    try:
        pf = env.tmp / "data" / "server.pid"
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(f"{proc.pid}\n8791\n{psutil.Process(proc.pid).create_time() - 3600}\n", encoding="ascii")
        r = env.run("stop")
        assert r.exit_code == 0 and "stale" in r.stdout
        time.sleep(0.3)
        assert proc.poll() is None and not pf.exists()
    finally:
        proc.kill()


def test_stop_with_dead_pid_removes_the_stale_file(env):
    proc = _sleeper("orchestrator.cli.main", "serve")
    pid = proc.pid
    proc.kill()
    proc.wait()
    server_mod.pid_file().parent.mkdir(parents=True, exist_ok=True)
    server_mod.pid_file().write_text(f"{pid}\n8791\n", encoding="ascii")
    r = env.run("stop")
    assert r.exit_code == 0 and not server_mod.pid_file().exists()


# ---- doctor ------------------------------------------------------------------------------------------
def _fake_versions(missing=()):
    return lambda exe, *a: None if exe in missing else f"{exe} 1.2.3"


def test_doctor_reports_checks_and_fixes(env, monkeypatch):
    monkeypatch.setattr(doctor_mod, "tool_version", _fake_versions(missing=("git",)))
    monkeypatch.setattr(doctor_mod, "detect_providers", lambda: {
        "claude": ProviderStatus(provider="claude", installed=True, version="2.1", authenticated=True),
        "codex": ProviderStatus(provider="codex", installed=False, error="codex not found on PATH"),
        "antigravity": ProviderStatus(provider="antigravity", installed=True, version="1.5", authenticated=False),
    })
    monkeypatch.setattr(doctor_mod, "real_providers_disabled", lambda: False)
    r = env.run("doctor")
    assert r.exit_code == 1  # git missing is a required check
    assert "how to fix" in r.stdout and "git-scm.com" in r.stdout
    checks = {c["check"]: c for c in json.loads(env.run("doctor", "--json").stdout)}
    assert checks["git"]["status"] == "FAIL"
    assert checks["provider claude"]["status"] == "ok"
    assert checks["provider codex"]["status"] == "warn" and checks["provider antigravity"]["status"] == "warn"
    assert checks["data dir writable"]["status"] == "ok"
    assert checks["port / server"]["status"] == "ok" and "answering" in checks["port / server"]["detail"]
    assert "frontend build" in checks and "api token file" in checks


def test_doctor_all_green_exits_0(env, monkeypatch):
    monkeypatch.setattr(doctor_mod, "tool_version", _fake_versions())
    monkeypatch.setattr(doctor_mod, "detect_providers", lambda: {})
    monkeypatch.setattr(doctor_mod, "real_providers_disabled", lambda: False)
    assert env.run("doctor").exit_code == 0


def test_doctor_flags_a_foreign_process_on_the_port(env, monkeypatch):
    cli_client.set_client_factory(canned_factory(_unreachable))
    monkeypatch.setattr(doctor_mod, "tool_version", _fake_versions())
    monkeypatch.setattr(doctor_mod, "detect_providers", lambda: {})
    monkeypatch.setattr(doctor_mod, "real_providers_disabled", lambda: False)
    monkeypatch.setattr(server_mod, "port_in_use", lambda port, host="127.0.0.1": True)
    r = env.run("doctor", "--json")
    checks = {c["check"]: c for c in json.loads(r.stdout)}
    assert r.exit_code == 1 and checks["port / server"]["status"] == "FAIL"


def test_serve_reports_a_busy_data_dir_in_one_line(env, monkeypatch):
    import uvicorn

    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
    monkeypatch.setenv("ORCHESTRATOR_BIND_PORT", "")
    # The env fixture's in-process app already holds the lock on this data dir: exactly the "second instance" case.
    r = env.run("serve", "--no-browser")
    assert r.exit_code == 1 and not calls
    assert "already using this data directory" in r.stderr and "Traceback" not in r.stderr
