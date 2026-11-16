"""M9a: structured leader checks (schemas, verifier kinds, safety filter) and the verification approval path for
objective-decomposed tasks."""

from __future__ import annotations

import copy
import json

import pytest

from orchestrator.domain.states import TaskState as S
from orchestrator.leader.safety import (
    assess_check,
    assess_checks,
    assess_proposal,
    assess_verify_commands,
    verify_prompt_block,
)
from orchestrator.leader.schemas import (
    DECOMPOSITION_SCHEMA,
    PLAN_SCHEMA,
    CheckSpec,
    Decomposition,
    PlanResult,
)
from orchestrator.testing import git, make_git_repo

from .conftest import GOOD_DECOMPOSITION, GOOD_PLAN
from .test_leader_objectives import task as dtask


# ------------------------------------------------------------------ schema validation
def test_check_spec_accepts_each_kind_and_rejects_ambiguous_ones():
    ok = [
        {"file_exists": "a.txt"},
        {"file_exists": "a.txt", "contains": "OK"},
        {"file_absent": "tmp.txt"},
        {"file_unchanged": "README.md"},
        {"json_valid": "package.json"},
        {"cmd": ["pytest", "-q"]},
        {"file_exists": None, "contains": None, "file_absent": None, "file_unchanged": None, "json_valid": None, "cmd": ["pytest"]},
    ]
    for c in ok:
        spec = CheckSpec.model_validate(c).to_spec()
        assert None not in spec.values()
    bad = [
        {},
        {"file_exists": "a", "file_absent": "b"},
        {"contains": "x"},
        {"file_absent": "a", "contains": "x"},
        {"cmd": []},
        {"cmd": "pytest -q"},  # shell strings are not a check
        {"file_exists": "a", "bogus": 1},
        {"file_exists": 5},
    ]
    for c in bad:
        with pytest.raises(ValueError):
            CheckSpec.model_validate(c)


def test_plan_and_decomposition_schemas_carry_checks_and_stay_backward_compatible():
    assert "checks" in PLAN_SCHEMA["required"] and "checks" in PLAN_SCHEMA["properties"]
    item = DECOMPOSITION_SCHEMA["properties"]["tasks"]["items"]
    assert "checks" in item["required"]
    props = item["properties"]["checks"]["items"]
    assert props["additionalProperties"] is False and set(props["required"]) == set(props["properties"])
    # a legacy answer without `checks` still validates
    assert PlanResult.model_validate(GOOD_PLAN).checks == []
    d = copy.deepcopy(GOOD_DECOMPOSITION)
    assert Decomposition.model_validate(d).tasks[0].checks == []
    d["tasks"][0]["checks"] = [{"file_exists": "hello.txt", "contains": "OK"}]
    assert Decomposition.model_validate(d).tasks[0].checks[0].to_spec() == {"file_exists": "hello.txt", "contains": "OK"}
    d["tasks"][0]["checks"] = [{"file_exists": "a", "cmd": ["pytest"]}]
    with pytest.raises(ValueError):
        Decomposition.model_validate(d)


# ------------------------------------------------------------------ verifier: every kind, real temp git repo
@pytest.fixture
def repo(tmp_path):
    return make_git_repo(tmp_path / "repo", {"README.md": "# readme\n", "data.json": '{"a": 1}\n', "bad.json": "{nope"})


async def _run(lh, repo, specs, base="HEAD"):
    base_sha = git(repo, "rev-parse", base).strip()
    return await lh.executor.verifier.run_checks(specs, str(repo), base_ref=base_sha)


async def test_file_exists_and_contains(lh, repo):
    (repo / "hello.txt").write_text("OK", encoding="utf-8")
    status, res = await _run(lh, repo, [{"file_exists": "hello.txt", "contains": "OK"}, {"file_exists": "hello.txt"}])
    assert status == "passed" and all(r.passed for r in res)
    status, res = await _run(lh, repo, [{"file_exists": "hello.txt", "contains": "NOPE"}])
    assert status == "failed" and "not found" in res[0].error
    status, res = await _run(lh, repo, [{"file_exists": "missing.txt"}])
    assert status == "failed" and "does not exist" in res[0].error


async def test_file_absent(lh, repo):
    status, res = await _run(lh, repo, [{"file_absent": "gone.txt"}])
    assert status == "passed" and res[0].kind == "file" and res[0].name == "absent:gone.txt"
    status, res = await _run(lh, repo, [{"file_absent": "README.md"}])
    assert status == "failed" and "must be absent" in res[0].error


async def test_file_unchanged_uses_the_base_commit(lh, repo):
    ok, _ = (await _run(lh, repo, [{"file_unchanged": "README.md"}]))[0], None
    assert ok == "passed"
    (repo / "README.md").write_text("# changed\n", encoding="utf-8")
    status, res = await _run(lh, repo, [{"file_unchanged": "README.md"}, {"file_unchanged": "data.json"}])
    assert status == "failed" and [r.passed for r in res] == [False, True]
    assert "differs" in res[0].error
    # a file deleted since the base commit is a change; a file that never existed is unchanged; a new file is a change
    (repo / "data.json").unlink()
    (repo / "new.txt").write_text("x", encoding="utf-8")
    status, res = await _run(lh, repo, [{"file_unchanged": "data.json"}, {"file_unchanged": "never.txt"}, {"file_unchanged": "new.txt"}])
    assert [r.passed for r in res] == [False, True, False]
    # restored content passes again (and CRLF normalisation is git's, not ours)
    (repo / "README.md").write_text("# readme\n", encoding="utf-8")
    assert (await _run(lh, repo, [{"file_unchanged": "README.md"}]))[0] == "passed"


async def test_file_unchanged_compares_with_the_task_base_not_head(lh, repo):
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "README.md").write_text("# second\n", encoding="utf-8")
    git(repo, "commit", "-aq", "-m", "second")  # committed changes still differ from the BASE commit
    status, res = await lh.executor.verifier.run_checks([{"file_unchanged": "README.md"}], str(repo), base_ref=base)
    assert status == "failed"
    status, _ = await lh.executor.verifier.run_checks([{"file_unchanged": "README.md"}], str(repo), base_ref="HEAD")
    assert status == "passed"


async def test_json_valid(lh, repo):
    status, res = await _run(lh, repo, [{"json_valid": "data.json"}])
    assert status == "passed"
    status, res = await _run(lh, repo, [{"json_valid": "bad.json"}, {"json_valid": "nope.json"}])
    assert [r.passed for r in res] == [False, False] and "not valid JSON" in res[0].error


async def test_cmd_list_form_and_path_validation(lh, repo):
    import sys

    from orchestrator.gitops import worktrees

    command = {"cmd": [sys.executable, "-c", "print('command-list-ok')"]}
    status, res = await _run(lh, repo, [command])
    assert status == "error" and res[0].error.startswith("sandbox_unavailable:")
    task_tree = lh.ctx.paths.worktrees_dir / "command-list"
    worktrees.create(repo, task_tree, "command-list", git(repo, "rev-parse", "HEAD").strip())
    # Command-list parsing needs a real owned task; Git's external administrative
    # directory deliberately does not receive a child read grant.
    status, res = await _run(lh, task_tree, [command])
    assert status == "passed" and res[0].kind == "command"
    assert "command-list-ok" in res[0].output
    for kind in ("file_exists", "file_absent", "file_unchanged", "json_valid"):
        status, res = await _run(lh, repo, [{kind: "../outside.txt"}])
        assert status == "failed" and "relative" in res[0].error, kind
    # a check with two kinds, or contains without file_exists, is an error entry, never silently run
    status, res = await _run(lh, repo, [{"file_exists": "a", "file_absent": "b"}, {"file_absent": "a", "contains": "x"}])
    assert status == "error" and len(res) == 2 and not any(r.passed for r in res)


# ------------------------------------------------------------------ safety filter table
def test_safety_filter_table_commands(tmp_path):
    roots = [str(tmp_path)]
    accepted = [
        "pytest -q", ["pytest", "-q", "tests"], "uv run pytest -q", ["uv", "run", "pytest"], "npm test", "npm.cmd test", "npm run lint",
        "npm.cmd run build", "npx.cmd vitest run", "npx.cmd vitest", "npx.cmd playwright test", "node --test", "go test ./...",
        "cargo test", "dotnet test", "python -m pytest", "git diff --exit-code README.md", ["git", "diff", "--exit-code"],
        "git status --porcelain", "git ls-files", "git ls-files src",
    ]
    rejected = {
        'python -c "print(1)"': "allowed as",
        'python3 -c "import os"': "allowed as",
        "node -e \"1\"": "node is only allowed",
        "node --eval x": "node is only allowed",
        "node script.js": "node is only allowed",
        "pytest -p evil": "execute code",
        "pytest -c other.ini": "execute code",
        "go test -c": "execute code",
        "git diff --exit-code --stat": "not allowed here",
        "git diff": "git is only allowed",
        "git log": "git is only allowed",
        "git commit -m x": "git is only allowed",
        "git status": "git is only allowed",
        "git status --porcelain --ignored": "git is only allowed",
        "git ls-files -z": "not allowed here",
        "git -c core.pager=x diff --exit-code": "git is only allowed",
        "npm install": "npm is only allowed",
        "npm.cmd run deploy": "npm is only allowed",
        "npx.cmd evil": "npx is only allowed",
        "npx.cmd -y vitest": "npx is only allowed",
        "dotnet run": "dotnet is only allowed",
        "pytest -q | tee x": "metacharacters",
        "cat README.md": "allowlist",
        "powershell -c x": "allowlist",
        "uv run --with x pytest": "options",
        "pytest ../../etc": "outside",
        "git diff --exit-code C:/Windows/win.ini": "outside",
        "git ls-files /etc": "outside",
    }
    for i in range(0, len(accepted), 8):  # at most 8 commands per call
        a = assess_verify_commands(accepted[i : i + 8], roots)
        assert a.unsafe == [] and len(a.safe) == len(accepted[i : i + 8]), a.unsafe
    got = {}
    for cmd, why in rejected.items():
        r = assess_verify_commands([cmd], roots)
        assert r.safe == [], cmd
        got[cmd] = r.unsafe[0]["reason"]
        assert got[cmd], cmd  # the exact wording is per-tool; the bypass table lives in test_m9a_security.py
    # argv-list form is validated argument by argument too
    assert assess_verify_commands([["pytest", "-q", "&&", "x"]], roots).unsafe
    assert assess_verify_commands([["pytest", "C:\\Windows\\x"]], roots).unsafe
    assert assess_verify_commands([[]], roots).unsafe


def test_safety_filter_structured_checks(tmp_path):
    roots = [str(tmp_path)]
    good = [
        {"file_exists": "hello.txt"}, {"file_exists": "dir/a.txt", "contains": "OK"}, {"file_absent": "tmp.txt"},
        {"file_unchanged": "README.md"}, {"json_valid": "package.json"}, {"cmd": ["pytest", "-q"]},
        {"cmd": ["git", "diff", "--exit-code", "README.md"]}, {"file_exists": "a.txt", "contains": None, "cmd": None},
    ]
    r = assess_checks(good, roots)
    assert r.unsafe == [] and len(r.safe) == len(good) and all(None not in c.values() for c in r.safe)
    bad = {
        "abs": ({"file_exists": "C:/Windows/win.ini"}, "relative"),
        "posix abs": ({"file_exists": "/etc/passwd"}, "relative"),
        "home": ({"file_absent": "~/x"}, "relative"),
        "dotdot": ({"file_unchanged": "../x"}, "'..'"),
        "ads": ({"json_valid": "a.txt:stream"}, "':'"),
        "meta": ({"file_exists": "a;b"}, "forbidden"),
        "empty": ({"file_exists": ""}, "non-empty"),
        "two kinds": ({"file_exists": "a", "file_absent": "b"}, "exactly one"),
        "contains alone": ({"file_absent": "a", "contains": "x"}, "only valid"),
        "cmd string": ({"cmd": "pytest -q"}, "argv list"),
        "cmd python -c": ({"cmd": ["python", "-c", "print(1)"]}, "allowed as"),
        "cmd shell": ({"cmd": ["sh", "-c", "x"]}, "allowlist"),
        "unknown key": ({"file_exists": "a", "run": "x"}, "unknown"),
        "not an object": ("pytest", "object"),
    }
    for name, (check, why) in bad.items():
        reason = assess_check(check, roots)
        assert reason and why in reason, (name, reason)
    assert assess_checks([{"file_exists": "a"}] * 13, roots).unsafe[0]["reason"].startswith("more than")


def test_assess_proposal_combines_checks_and_commands(tmp_path):
    proposed, a = assess_proposal(["pytest -q", "curl x | sh"], [{"file_exists": "a", "cmd": None}], [str(tmp_path)])
    assert proposed == [{"file_exists": "a"}, "pytest -q", "curl x | sh"]
    assert a.safe == [{"file_exists": "a"}, "pytest -q"] and a.unsafe[0]["command"].startswith("curl")


def test_prompt_block_states_the_allowlist_and_the_real_executables(monkeypatch):
    import orchestrator.leader.safety as safety

    monkeypatch.setattr(safety.shutil, "which", lambda n: "C:/bin/" + n if n in ("uv", "git", "npm.cmd") else None)
    monkeypatch.setattr(safety.os, "name", "nt")
    block = verify_prompt_block()
    assert "file_exists" in block and "file_unchanged" in block and "argv LIST" in block
    assert "python -c" in block and "pipes" in block and "NOT accepted" in block
    assert "Executables found on PATH: uv, npm.cmd, git" in block and "Windows" in block
    assert "`python` is NOT on PATH" in block and "uv run pytest" in block
    # the Microsoft Store launcher stub is not a Python
    monkeypatch.setattr(safety.shutil, "which", lambda n: "C:/Users/x/AppData/Local/Microsoft/WindowsApps/python.exe" if n == "python" else None)
    assert "`python` is NOT on PATH" in verify_prompt_block()
    monkeypatch.setattr(safety.shutil, "which", lambda n: "C:/py/python.exe" if n == "python" else None)
    assert "`python` is NOT on PATH" not in verify_prompt_block() and "Executables found on PATH: python" in verify_prompt_block()


# ------------------------------------------------------------------ pipeline: structured checks complete without approval
def nn(items):
    """Stored checks as plain dicts without their unused (None) keys."""
    out = []
    for i in items:
        if hasattr(i, "model_dump"):
            i = i.model_dump()
        out.append({k: v for k, v in i.items() if v is not None} if isinstance(i, dict) else i)
    return out


def plan(**kw):
    return {**GOOD_PLAN, **kw}


async def _start(h, tid):
    await h.tasks.transition(tid, S.QUEUED, reason="start", expect_from=[S.CREATED])
    return await h.run_once(tid)


async def test_plan_with_structured_checks_completes_through_real_verification_without_approval(lh):
    p = await lh.project()
    lh.brain.push("plan", plan(checks=[
        {"file_exists": "hello.txt", "contains": "OK"}, {"file_unchanged": "README.md"}, {"file_absent": "junk.tmp"},
    ]))
    t = await lh.mk_task(p.id, title="add hello", description="write OK into hello.txt")
    state = await _start(lh, t.id)
    t = await lh.tasks.get(t.id)
    assert state == S.COMPLETED, t.state_reason
    assert nn(t.verify_commands) == [{"file_exists": "hello.txt", "contains": "OK"}, {"file_unchanged": "README.md"}, {"file_absent": "junk.tmp"}]
    assert await lh.approvals.list("pending", t.id) == [] and await lh.approvals.list("approved", t.id) == []
    v = await lh.ctx.db.read(lambda tx: tx.one("SELECT status, checks FROM verifications WHERE task_id = ?", (t.id,)))
    assert v["status"] == "passed" and len(json.loads(v["checks"])) == 3
    assert "file_unchanged" in lh.brain.prompts["plan"][0] and "Executables found on PATH" in lh.brain.prompts["plan"][0]


async def test_failing_structured_check_fails_verification_not_the_task_silently(lh):
    p = await lh.project()
    lh.brain.push("plan", plan(checks=[{"file_exists": "hello.txt", "contains": "NOT-THERE"}]))
    t = await lh.mk_task(p.id, title="add hello")
    state = await _start(lh, t.id)
    assert state == S.FAILED  # repairs exhaust; completion needs passing verification
    v = await lh.ctx.db.read(lambda tx: tx.one("SELECT status FROM verifications WHERE task_id = ? ORDER BY started_at DESC", (t.id,)))
    assert v["status"] == "failed"


async def test_unsafe_check_in_a_plan_requests_approval_listing_everything_proposed(lh):
    p = await lh.project()
    lh.brain.push("plan", plan(checks=[{"file_exists": "hello.txt"}, {"cmd": ["python", "-c", "print(1)"]}]))
    t = await lh.mk_task(p.id, title="t")
    state = await _start(lh, t.id)
    assert state == S.NEEDS_APPROVAL
    [apr] = await lh.approvals.list("pending", t.id)
    assert apr.kind == "leader_verify_commands" and apr.details["verify_commands"][0] == {"file_exists": "hello.txt"}
    assert apr.details["unsafe"][0]["reason"].startswith("python is only allowed")
    assert (await lh.tasks.get(t.id)).verify_commands == []
    await lh.approvals.resolve(apr.id, True)
    assert nn((await lh.tasks.get(t.id)).verify_commands) == apr.details["verify_commands"]


# ------------------------------------------------------------------ pipeline: objective-decomposed tasks
def _decomp(*tasks):
    d = copy.deepcopy(GOOD_DECOMPOSITION)
    d["tasks"] = list(tasks)
    return d


async def test_decomposed_task_with_structured_checks_completes_without_approval(lh):
    p = await lh.project()
    lh.brain.push("decompose", _decomp(dtask("a", checks=[{"file_exists": "hello.txt", "contains": "OK"}, {"file_unchanged": "README.md"}])))
    obj = await lh.stack.objectives.create(p.id, "Add hello.txt", wait=True)
    [row] = obj["tasks"]
    assert nn(row["verify_commands"]) == [{"file_exists": "hello.txt", "contains": "OK"}, {"file_unchanged": "README.md"}]
    assert obj["unsafe_verify_commands"] == {}
    assert "file_unchanged" in lh.brain.prompts["decompose"][0] and "Executables found on PATH" in lh.brain.prompts["decompose"][0]
    state = await _start(lh, row["id"])
    t = await lh.tasks.get(row["id"])
    assert state == S.COMPLETED, t.state_reason
    assert await lh.approvals.list("pending", t.id) == []
    v = await lh.ctx.db.read(lambda tx: tx.one("SELECT status FROM verifications WHERE task_id = ?", (t.id,)))
    assert v["status"] == "passed"


async def test_decomposed_task_with_unsafe_verification_asks_for_the_verify_approval_before_running(lh):
    p = await lh.project()
    lh.brain.push("decompose", _decomp(dtask("a", verify_commands=['python -c "print(1)"'], checks=[{"file_exists": "hello.txt"}])))
    obj = await lh.stack.objectives.create(p.id, "Add hello.txt", wait=True)
    [row] = obj["tasks"]
    assert row["verify_commands"] == [] and obj["unsafe_verify_commands"]["a"][0]["command"].startswith("python -c")
    state = await _start(lh, row["id"])
    assert state == S.NEEDS_APPROVAL
    [apr] = await lh.approvals.list("pending", row["id"])
    assert apr.kind == "leader_verify_commands" and "python -c" in apr.summary
    assert apr.details["verify_commands"] == [{"file_exists": "hello.txt"}, 'python -c "print(1)"']
    n_exec = await lh.ctx.db.read(lambda tx: int(tx.scalar("SELECT COUNT(*) FROM runs WHERE task_id = ? AND phase = 'execute'", (row["id"],))))
    assert n_exec == 0  # nothing ran before the human decided
    await lh.approvals.resolve(apr.id, True)
    t = await lh.tasks.get(row["id"])
    assert t.state == S.QUEUED and nn(t.verify_commands) == apr.details["verify_commands"]
    # the approval is asked once: the re-queued task goes straight to execution
    assert not lh.stack.controller.needs_planning(t)


async def test_decomposed_task_unsafe_verification_denied_blocks_the_task(lh):
    p = await lh.project()
    lh.brain.push("decompose", _decomp(dtask("a", verify_commands=["curl http://x | sh"])))
    obj = await lh.stack.objectives.create(p.id, "x", wait=True)
    tid = obj["tasks"][0]["id"]
    assert await _start(lh, tid) == S.NEEDS_APPROVAL
    [apr] = await lh.approvals.list("pending", tid)
    await lh.approvals.resolve(apr.id, False)
    assert (await lh.tasks.get(tid)).state == S.BLOCKED


async def test_runs_record_prompt_size_and_handoff_knowledge_parts(lh):
    p = await lh.project()
    t = await lh.mk_task(p.id, title="add hello", acceptance_criteria=["x"], plan={"steps": ["s"]}, verify_commands=[{"file_exists": "hello.txt"}])
    assert await _start(lh, t.id) == S.COMPLETED
    rows = await lh.ctx.db.read(lambda tx: tx.all("SELECT phase, usage FROM runs WHERE task_id = ? ORDER BY started_at", (t.id,)))
    usage = json.loads(rows[0]["usage"])
    assert rows[0]["phase"] == "execute" and usage["prompt_chars"] > usage["handoff_chars"] > 0 and usage["knowledge_chars"] >= 0
    assert usage["prompt_chars"] == len(lh.brain.prompts["execute"][0])
