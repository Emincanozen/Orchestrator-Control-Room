"""M9a security-review fixes: allowlist bypasses (finding 2), trivially passing leader checks (9), verifier resource
handling (11) and the .cmd/.bat name filter (14)."""

from __future__ import annotations

import os
import sys

import psutil
import pytest

from orchestrator.domain.states import TaskState as S
from orchestrator.leader.safety import (
    assess_command,
    assess_proposal,
    has_positive,
    is_positive_check,
)
from orchestrator.scheduler import verification as vmod
from orchestrator.scheduler.verification import _is_cmd_script, run_command

from .conftest import GOOD_PLAN
from .test_structured_checks import _decomp, _start, nn
from .test_leader_objectives import task as dtask


# ------------------------------------------------------------------ finding 2: default-deny argument allowlists
REJECTED = [
    # (a) path arguments
    ["pytest", "--basetemp=C:Users"], ["pytest", "--basetemp=x/..\\.."], ["pytest", "--basetemp=x/.."],
    ["pytest", "--basetemp", "x"], ["pytest", "--rootdir=."], ["pytest", "--confcutdir=."], ["pytest", "-o", "addopts=x"],
    ["pytest", "-c", "x.ini"], ["pytest", "-p", "evil"], ["pytest", "C:Users"], ["pytest", "x/..\\.."], ["pytest", "..\\x"],
    ["pytest", "C:/Windows/x.py"], ["pytest", "tests/../../x.py"], ["pytest", "tests/a.py::t", "C:/Windows/x.py"],
    ["git", "diff", "--exit-code", "C:Users"], ["git", "ls-files", "../x"], ["node", "--test", "C:\\x"],
    # (b) argv[0]
    ["tools/pytest.bat"], ["C:/Users/Public/pytest.exe"], ["pytest.bat"], ["pytest.exe."], [" pytest"], ["..\\pytest"],
    ["./pytest"], ["pytest.ps1"], ["uv", "run", "tools/pytest.bat"], ["uv", "run", "./pytest"],
    # (c) code-running flags on allowlisted tools
    ["go", "test", "-exec=cmd /c calc"], ["go", "test", "-exec", "calc"], ["go", "test", "-toolexec=x"], ["go", "test", "-c"],
    ["cargo", "test", "--config", "target.x86_64-pc-windows-msvc.runner=cmd /c calc"], ["cargo", "test", "--config=x"],
    ["node", "--test", "--inspect=0.0.0.0:9229"], ["node", "--test", "-r", "./x.js"], ["node", "--test", "--require=x"],
    ["node", "-e", "1"], ["node", "x.js"], ["pytest", "-pevil"], ["pytest", "-p", "no:cacheprovider"],
    ["npm", "test", "--script-shell=powershell"], ["npm", "test", "--", "--x"], ["npm", "run", "test", "--script-shell=x"],
    ["npm", "run", "deploy"], ["npm", "install"], ["npm.cmd", "run", "lint", "extra"], ["npx.cmd", "-y", "vitest"], ["npx.cmd", "evil"],
    ["dotnet", "run"], ["dotnet", "test", "--logger", "x"], ["git", "diff", "--exit-code", "--ext-diff"], ["git", "log"],
    ["git", "status"], ["git", "status", "--porcelain", "-z"], ["uv", "run", "--with", "x", "pytest"], ["uv", "run", "uv", "x"],
    ["python", "-c", "print(1)"], ["python", "-m", "http.server"], ["cargo", "install", "x"], ["go", "run", "."],
    ["pytest", "-k"], ["pytest", "-k", "a;b"],
]
ACCEPTED = [
    ["pytest"], ["pytest", "-q"], ["pytest", "-q", "-x", "tests"], ["pytest", "tests/test_a.py::TestX::test_y"],
    ["pytest", "-k", "alpha and not beta", "-m", "slow", "--maxfail=3", "-v", "--tb=short"], ["pytest.exe", "-q"],
    ["uv", "run", "pytest", "-q"], ["uv", "run", "python", "-m", "pytest", "tests"], ["python", "-m", "pytest", "-q"],
    ["py", "-m", "unittest", "-v"], ["npm", "test"], ["npm.cmd", "test"], ["npm", "run", "lint"], ["npm.cmd", "run", "build"],
    ["npx.cmd", "vitest", "run"], ["npx.cmd", "vitest", "run", "src/a.test.ts"], ["npx.cmd", "playwright", "test"],
    ["node", "--test"], ["node", "--test", "test/a.test.js"], ["go", "test", "./...", "-run", "TestX", "-count=1", "-v"],
    ["go", "test"], ["go", "vet", "./..."], ["cargo", "test"], ["cargo", "test", "--lib"], ["cargo", "test", "--tests", "my_test"],
    ["dotnet", "test"], ["git", "diff", "--exit-code"], ["git", "diff", "--exit-code", "README.md", "src/a.py"],
    ["git", "status", "--porcelain"], ["git", "ls-files"], ["git", "ls-files", "src"],
]


@pytest.mark.parametrize("argv", REJECTED, ids=lambda a: " ".join(a)[:70])
def test_bypass_attempts_and_unlisted_forms_are_rejected(tmp_path, argv):
    assert assess_command(argv, [str(tmp_path)]) is not None


@pytest.mark.parametrize("argv", ACCEPTED, ids=lambda a: " ".join(a)[:70])
def test_allowlisted_forms_are_accepted(tmp_path, argv):
    assert assess_command(argv, [str(tmp_path)]) is None, assess_command(argv, [str(tmp_path)])


def test_string_form_follows_the_same_rules(tmp_path):
    roots = [str(tmp_path)]
    assert assess_command("pytest -q tests", roots) is None
    assert assess_command("pytest --basetemp=C:Users", roots) is not None
    assert assess_command("tools/pytest.bat", roots) is not None
    assert assess_command("go test -exec=calc", roots) is not None


def test_absolute_paths_inside_the_worktree_are_fine_outside_are_not(tmp_path):
    inside = str(tmp_path / "tests" / "a.py")
    assert assess_command(["pytest", inside], [str(tmp_path)]) is None
    assert assess_command(["pytest", str(tmp_path.parent)], [str(tmp_path)]) is not None


# ------------------------------------------------------------------ finding 9: positive check required
def test_positive_check_classification():
    assert is_positive_check({"file_exists": "a.txt"}) and is_positive_check({"file_exists": "a", "contains": "x"})
    assert is_positive_check({"json_valid": "a.json"})
    assert is_positive_check({"cmd": ["pytest", "-q"]}) and is_positive_check("npm test") and is_positive_check(["go", "test"])
    for weak in ({"file_absent": "nonexistent"}, {"file_unchanged": "README.md"}, {"cmd": ["git", "status", "--porcelain"]},
                 {"cmd": ["git", "ls-files"]}, {"cmd": ["git", "diff", "--exit-code"]}, "git status --porcelain", "npm run lint",
                 "cargo build", "go vet ./...", {"cmd": ["python", "-m", "compileall", "."]}):
        assert not is_positive_check(weak), weak
    assert not has_positive([{"file_absent": "x"}, "git ls-files"]) and has_positive([{"file_absent": "x"}, {"file_exists": "y"}])


def test_assess_proposal_keeps_weak_checks_safe_but_not_sufficient(tmp_path):
    proposed, a = assess_proposal([], [{"file_absent": "nonexistent"}, {"cmd": ["git", "status", "--porcelain"]}], [str(tmp_path)])
    assert a.unsafe == [] and len(a.safe) == 2 and not has_positive(a.safe)


async def test_plan_with_only_trivial_checks_is_not_adopted_and_needs_the_unverifiable_approval(lh):
    p = await lh.project()
    lh.brain.push("plan", {**GOOD_PLAN, "checks": [{"file_absent": "nonexistent"}], "verify_commands": ["git status --porcelain"]})
    t = await lh.mk_task(p.id, title="add hello")
    state = await _start(lh, t.id)
    t = await lh.tasks.get(t.id)
    assert t.verify_commands == [] and "no positive check" in t.plan["verify_not_adopted"]
    assert state == S.NEEDS_APPROVAL
    [apr] = await lh.approvals.list("pending", t.id)
    assert apr.kind == "unverifiable_completion"  # not completed on the strength of trivially passing checks


async def test_plan_with_weak_plus_positive_checks_is_adopted_with_origin_leader(lh):
    p = await lh.project()
    lh.brain.push("plan", {**GOOD_PLAN, "checks": [{"file_absent": "junk"}, {"file_exists": "hello.txt", "contains": "OK"}]})
    t = await lh.mk_task(p.id, title="add hello")
    assert await _start(lh, t.id) == S.COMPLETED
    t = await lh.tasks.get(t.id)
    assert t.plan["verify_origin"] == "leader" and len(t.verify_commands) == 2
    v = await lh.ctx.db.read(lambda tx: tx.one("SELECT origin, status FROM verifications WHERE task_id = ?", (t.id,)))
    assert (v["origin"], v["status"]) == ("leader", "passed")
    ev = [e for e in lh.events("verification.result") if e["task_id"] == t.id]
    assert ev[-1]["payload"]["origin"] == "leader"


async def test_decomposed_task_with_only_weak_checks_is_not_adopted(lh):
    p = await lh.project()
    lh.brain.push("decompose", _decomp(dtask("a", checks=[{"file_unchanged": "README.md"}], verify_commands=["git ls-files"])))
    obj = await lh.stack.objectives.create(p.id, "x", wait=True)
    [row] = obj["tasks"]
    assert row["verify_commands"] == []
    assert "no positive check" in (await lh.tasks.get(row["id"])).plan["verify_not_adopted"]
    assert await _start(lh, row["id"]) == S.NEEDS_APPROVAL
    [apr] = await lh.approvals.list("pending", row["id"])
    assert apr.kind == "unverifiable_completion"


async def test_user_defined_and_approved_verification_origins(lh):
    p = await lh.project()
    t = await lh.mk_task(p.id, title="t", acceptance_criteria=["x"], plan={"steps": ["s"]}, verify_commands=[{"file_exists": "hello.txt"}])
    assert await _start(lh, t.id) == S.COMPLETED
    v = await lh.ctx.db.read(lambda tx: tx.one("SELECT origin FROM verifications WHERE task_id = ?", (t.id,)))
    assert v["origin"] == "user"
    # a human-approved leader list is marked "approved"
    lh.brain.push("plan", {**GOOD_PLAN, "checks": [{"file_exists": "hello.txt"}, {"cmd": ["python", "-c", "1"]}]})
    t2 = await lh.mk_task(p.id, title="t2")
    assert await _start(lh, t2.id) == S.NEEDS_APPROVAL
    [apr] = await lh.approvals.list("pending", t2.id)
    await lh.approvals.resolve(apr.id, True)
    assert (await lh.tasks.get(t2.id)).plan["verify_origin"] == "approved"
    assert await lh.run_once(t2.id) == S.COMPLETED
    v2 = await lh.ctx.db.read(lambda tx: tx.one("SELECT origin FROM verifications WHERE task_id = ? ORDER BY rowid DESC", (t2.id,)))
    assert v2["origin"] == "approved"


def test_verification_api_payload_carries_origin(tmp_path):
    from fastapi.testclient import TestClient

    from orchestrator.api.app import create_app
    from orchestrator.providers.mock import MockProvider
    from orchestrator.testing import make_git_repo

    app = create_app(data_dir=tmp_path / "data", providers={"mock": MockProvider()}, celebrate_seconds=0, poll_interval=0.05)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        h = {"Authorization": f"Bearer {app.state.rt.token}"}
        repo = make_git_repo(tmp_path / "repo")
        pid = c.post("/api/projects", json={"name": "p", "path": str(repo)}, headers=h).json()["id"]
        tid = c.post("/api/tasks", json={"project_id": pid, "title": "t", "preferred_provider": "mock", "acceptance_criteria": ["x"],
                                         "verify_commands": [{"file_exists": "README.md"}], "autostart": True}, headers=h).json()["id"]
        import time

        for _ in range(300):
            v = c.get(f"/api/tasks/{tid}/verification", headers=h).json()
            if v:
                break
            time.sleep(0.1)
        assert v and v[-1]["origin"] == "user" and v[-1]["status"] == "passed"


# ------------------------------------------------------------------ finding 11: verifier resource handling
def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


async def test_runaway_output_is_capped_and_the_command_stopped(monkeypatch, native_task):
    monkeypatch.setattr(vmod, "MAX_OUTPUT_BYTES", 200_000)
    r = await run_command(_py("import sys\nwhile True:\n    sys.stdout.write('x'*1000+'\\n'); sys.stdout.flush()"), str(native_task), 60, dict(os.environ), writable_cwd=True)
    assert not r.passed and "output exceeded" in (r.error or "") and not r.timed_out
    assert len(r.output) <= vmod.OUTPUT_LIMIT + 200  # only a bounded tail is ever kept


async def test_output_tail_is_bounded_not_the_whole_file(native_task):
    r = await run_command(_py("print('A'*50000); print('THE-END')"), str(native_task), 60, dict(os.environ), writable_cwd=True)
    assert r.passed and "THE-END" in r.output and len(r.output) <= vmod.OUTPUT_LIMIT + 200


async def test_grandchildren_are_killed_after_a_normal_exit(native_task):
    code = (
        "import subprocess,sys,time\n"
        "c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])\n"
        "print(c.pid, flush=True)\n"
        "time.sleep(1.0)\n"
    )
    r = await run_command(_py(code), str(native_task), 60, dict(os.environ), writable_cwd=True)
    assert r.passed
    child = int(r.output.split()[0])
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE


async def test_blocking_waits_do_not_stall_the_event_loop(native_task):
    import asyncio

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.create_task(ticker())
    try:
        await run_command(_py("import time; time.sleep(1.0)"), str(native_task), 60, dict(os.environ), writable_cwd=True)
    finally:
        t.cancel()
    assert ticks >= 20  # the loop kept running while the command ran and while it was reaped


# ------------------------------------------------------------------ finding 14: .cmd/.bat name filter
@pytest.mark.parametrize("name", ["x.bat", "x.BAT", "x.bat.", "x.bat ", "x.cmd.", "C:\\t\\x.cmd. .", "d/x.bat..."])
def test_batch_names_are_recognised_after_normalisation(name):
    assert _is_cmd_script(name)


@pytest.mark.parametrize("name", ["x.exe", "pytest", "x.batx", "bat"])
def test_non_batch_names_are_not_flagged(name):
    assert not _is_cmd_script(name)


async def test_strict_specs_refuse_a_directory_component_in_argv0():
    for argv0 in ("tools/pytest", "tools\\pytest.bat", "C:/Users/Public/pytest.exe", "x.bat."):
        r = await run_command([argv0, "-q"], os.getcwd(), 5, dict(os.environ), strict=True)
        assert not r.passed and "bare command name" in (r.error or ""), argv0


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe metacharacter filter is Windows-only")
async def test_cmd_script_with_trailing_dot_still_gets_the_metacharacter_filter(tmp_path):
    script = tmp_path / "echo.cmd"
    script.write_text("@echo hi\n")
    r = await run_command([str(script) + ".", "a&b"], str(tmp_path), 10, dict(os.environ))
    assert not r.passed  # either not found/started or refused for metacharacters, never executed through cmd.exe unfiltered
